import json
from types import SimpleNamespace

import pytest
import requests

from core.models.debater import Debater
from core.models.school import School
from core.utils.api_data import APIDataHandler


pytestmark = pytest.mark.django_db


@pytest.fixture
def api_request():
    return SimpleNamespace(session={})


@pytest.fixture
def handler(api_request):
    return APIDataHandler(api_request)


def test_set_api_url_normalizes_and_persists(handler, api_request):
    handler.set_api_url("apda.online/path")

    assert handler.get_api_url() == "https://apda.online"
    assert api_request.session["tournament_api_url"] == "https://apda.online"
    assert handler.should_use_api_data() is True


def test_clear_tournament_session_data_removes_expected_keys(api_request):
    api_request.session.update(
        {
            "tournament_api_url": "https://apda.online",
            "tournament_debater_mapping": {"1": 2},
            "tournament_id": 5,
            "unrelated": "keep-me",
        }
    )

    APIDataHandler.clear_tournament_session_data(api_request)

    assert api_request.session == {"unrelated": "keep-me"}


def test_validate_api_connection_handles_api_error(handler, monkeypatch):
    handler.set_api_url("https://apda.online")

    class ErrorResponse:
        status_code = 403

        def json(self):
            return {"error": "forbidden"}

        def raise_for_status(self):
            raise AssertionError("should not be called for handled status")

    monkeypatch.setattr(requests, "get", lambda url, timeout=10: ErrorResponse())

    ok, error = handler.validate_api_connection()

    assert ok is False
    assert "forbidden" in error


def test_validate_api_connection_handles_timeout(handler, monkeypatch):
    handler.set_api_url("https://apda.online")

    def raiser(url, timeout=10):  # pylint: disable=unused-argument
        raise requests.Timeout("slow")

    monkeypatch.setattr(requests, "get", raiser)

    ok, error = handler.validate_api_connection()

    assert ok is False
    assert "Failed to connect" in error


def test_make_api_request_returns_json(handler, monkeypatch):
    handler.set_api_url("https://apda.online")

    class JsonResponse:
        status_code = 200

        def __init__(self):
            self.content = json.dumps({"key": "value"}).encode()

        def raise_for_status(self):
            return None

        def json(self):
            return {"key": "value"}

    monkeypatch.setattr(requests, "get", lambda url, timeout=10: JsonResponse())

    payload = handler._make_api_request("new-debater-data")

    assert payload == {"key": "value"}


def test_make_api_request_handles_empty_payload(handler, monkeypatch):
    handler.set_api_url("https://apda.online")

    class EmptyResponse:
        status_code = 200
        content = b"  "

        def raise_for_status(self):
            return None

        def json(self):  # pragma: nocover - should not be invoked
            raise AssertionError("json should not be called when content empty")

    monkeypatch.setattr(requests, "get", lambda url, timeout=10: EmptyResponse())

    payload = handler._make_api_request("new-debater-data")

    assert payload is None


def test_get_new_schools_filters_existing(handler):
    existing = School.objects.create(name="Existing", included_in_oty=True)

    handler._make_api_request = lambda endpoint: {
        "new_schools": [existing.name, "New School"],
    }

    schools = handler.get_new_schools_from_api()

    assert schools == [{"name": "New School", "included_in_oty": True}]


def test_get_new_debaters_maps_by_id_and_name(handler):
    school_id_match = School.objects.create(name="Alpha", included_in_oty=True)
    school_name_match = School.objects.create(name="Beta", included_in_oty=True)

    handler._make_api_request = lambda endpoint: {
        "new_debater_data": [
            {
                "name": "Alice Anderson",
                "school_id": school_id_match.id,
                "school_name": "",
                "debater_id": 11,
            },
            {
                "name": "Bob Brown",
                "school_id": -1,
                "school_name": school_name_match.name,
                "debater_id": 12,
            },
            {
                "name": "   ",
                "school_id": -1,
                "school_name": "",
                "debater_id": 13,
            },
            {
                "name": "Cara Clark",
                "school_id": -1,
                "school_name": "Unknown",
                "debater_id": 14,
            },
        ]
    }

    debaters = handler.get_new_debaters_from_api()

    assert len(debaters) == 3

    first = debaters[0]
    assert first["first_name"] == "Alice"
    assert first["last_name"] == "Anderson"
    assert first["school"] == school_id_match

    second = debaters[1]
    assert second["first_name"] == "Bob"
    assert second["school"] == school_name_match

    third = debaters[2]
    assert third["first_name"] == "Cara"
    assert third["school"] is None


def test_create_schools_from_data_returns_queryset(handler):
    schools = handler.create_schools_from_data(
        [{"name": "Gamma", "included_in_oty": False}]
    )

    assert schools.count() == 1
    created = schools.first()
    assert created.name == "Gamma"
    assert created.included_in_oty is False


def test_create_debaters_from_data_persists_and_maps(handler, api_request):
    school = School.objects.create(name="Mapped", included_in_oty=True)

    created_count = handler.create_debaters_from_data(
        [
            {
                "first_name": "Dan",
                "last_name": "Davis",
                "school": school,
                "tournament_id": 101,
            },
            {"first_name": "No", "last_name": "School", "school": None},
        ]
    )

    assert created_count == 1
    saved = Debater.objects.get(first_name="Dan")
    assert api_request.session["tournament_debater_mapping"] == {"101": saved.id}
    assert handler._debater_id_map["101"] == saved.id


def test_link_tournament_debater_updates_session(handler, api_request):
    school = School.objects.create(name="Linked", included_in_oty=True)
    debater = Debater.objects.create(first_name="Link", last_name="Able", school=school)

    handler.link_tournament_debater(404, debater)

    assert api_request.session["tournament_debater_mapping"] == {"404": debater.id}
    assert handler._debater_id_map["404"] == debater.id


def test_create_debaters_uses_lookup_when_bulk_create_drops_ids(handler, monkeypatch):
    school = School.objects.create(name="Fallback", included_in_oty=True)

    original_bulk_create = Debater.objects.bulk_create

    def fake_bulk_create(objs, ignore_conflicts=False):
        original_bulk_create(objs, ignore_conflicts=ignore_conflicts)
        return [
            Debater(first_name=obj.first_name, last_name=obj.last_name, school=obj.school)
            for obj in objs
        ]

    monkeypatch.setattr(Debater.objects, "bulk_create", fake_bulk_create)

    handler.create_debaters_from_data(
        [
            {
                "first_name": "Eve",
                "last_name": "Evans",
                "school": school,
                "tournament_id": 202,
            }
        ]
    )

    saved = Debater.objects.get(first_name="Eve")
    assert handler._debater_id_map["202"] == saved.id


def test_find_debater_from_ref_prefers_direct_ids(handler):
    school = School.objects.create(name="Direct", included_in_oty=True)
    direct = Debater.objects.create(first_name="Fiona", last_name="Flynn", school=school)

    result = handler._find_debater_from_ref({"apda_id": direct.id})

    assert result == direct


def test_find_debater_from_ref_uses_cached_mapping(handler, api_request):
    school = School.objects.create(name="Cached", included_in_oty=True)
    linked = Debater.objects.create(first_name="Gina", last_name="Gray", school=school)

    api_request.session["tournament_debater_mapping"] = {"303": linked.id}
    handler._debater_id_map["303"] = linked.id

    result = handler._find_debater_from_ref({"apda_id": -1, "tournament_id": 303})

    assert result == linked


def test_find_debater_from_ref_rejects_invalid_data(handler):
    assert handler._find_debater_from_ref(None) is None
    assert handler._find_debater_from_ref({"apda_id": 999999}) is None
    assert handler._find_debater_from_ref({"tournament_id": 404}) is None
