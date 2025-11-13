import json
from types import SimpleNamespace
import pytest
import requests

from core.models.debater import Debater
from core.models.school import School
from core.utils.api_data import APIDataHandler
from django.conf import settings


pytestmark = pytest.mark.django_db


@pytest.fixture
def handler():
    return APIDataHandler(SimpleNamespace())


def test_set_api_url_normalizes_and_persists(handler):
    handler.set_api_url("apda.online/path")

    assert handler.get_api_url() == "https://apda.online"
    assert handler.should_use_api_data() is True




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

    assert schools == [
        {"name": "New School", "included_in_oty": True, "server_name": "New School"}
    ]


def test_get_new_debaters_maps_by_id_and_name(handler):
    school_id_match = School.objects.create(name="Alpha", included_in_oty=True)
    school_name_match = School.objects.create(name="Beta", included_in_oty=True)
    existing = Debater.objects.create(
        first_name="Alice",
        last_name="Anderson",
        school=school_id_match,
        latest_season=settings.CURRENT_SEASON,
    )

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

    assert len(debaters) == 2

    assert handler._debater_id_map["11"] == existing.id

    first = debaters[0]
    assert first["first_name"] == "Bob"
    assert first["school"] == school_name_match

    second = debaters[1]
    assert second["first_name"] == "Cara"
    assert second["school"] is None


def test_get_new_debaters_keeps_old_matches(handler):
    school = School.objects.create(name="Gamma", included_in_oty=True)
    Debater.objects.create(
        first_name="Harold",
        last_name="Hill",
        school=school,
        latest_season="2018",
    )

    handler._make_api_request = lambda endpoint: {
        "new_debater_data": [
            {
                "name": "Harold Hill",
                "school_id": school.id,
                "school_name": "",
                "debater_id": 201,
            }
        ]
    }

    debaters = handler.get_new_debaters_from_api()

    assert len(debaters) == 1
    assert debaters[0]["first_name"] == "Harold"
    assert "201" not in handler._debater_id_map


def test_get_teams_from_api_detects_debater_id(handler):
    handler._make_api_request = lambda endpoint: {
        "varsity_team_placements": [
            [
                {"debater_id": 301},
                {"debater_id": 302},
            ]
        ]
    }

    teams = handler.get_teams_from_api("varsity-team-placements")

    assert teams[0]["debater_one_tournament_id"] == 301
    assert teams[0]["debater_two_tournament_id"] == 302


def test_get_speakers_from_api_detects_debater_id(handler):
    handler._make_api_request = lambda endpoint: {
        "varsity_speaker_awards": [
            {"debater_id": 501},
        ]
    }

    speakers = handler.get_speakers_from_api("varsity-speaker-awards")

    assert speakers[0]["tournament_id"] == 501


def test_create_schools_from_data_returns_queryset(handler):
    schools = handler.create_schools_from_data(
        [{"name": "Gamma", "included_in_oty": False}]
    )

    assert schools.count() == 1
    created = schools.first()
    assert created.name == "Gamma"
    assert created.included_in_oty is False


def test_create_debaters_from_data_persists_and_maps(handler):
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
    assert handler._debater_id_map["101"] == saved.id


def test_link_tournament_debater_updates_session(handler):
    school = School.objects.create(name="Linked", included_in_oty=True)
    debater = Debater.objects.create(first_name="Link", last_name="Able", school=school)

    handler.link_tournament_debater(404, debater)

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


def test_find_debater_from_ref_supports_debater_id(handler):
    school = School.objects.create(name="DeRef", included_in_oty=True)
    linked = Debater.objects.create(first_name="Gale", last_name="Gray", school=school)
    handler._debater_id_map["808"] = linked.id

    assert handler._find_debater_from_ref({"debater_id": 808}) == linked


def test_find_debater_from_ref_uses_cached_mapping(handler):
    school = School.objects.create(name="Cached", included_in_oty=True)
    linked = Debater.objects.create(first_name="Gina", last_name="Gray", school=school)

    handler._debater_id_map["303"] = linked.id

    result = handler._find_debater_from_ref({"apda_id": -1, "tournament_id": 303})

    assert result == linked


def test_find_debater_from_ref_rejects_invalid_data(handler):
    assert handler._find_debater_from_ref(None) is None
    assert handler._find_debater_from_ref({"apda_id": 999999}) is None
    assert handler._find_debater_from_ref({"tournament_id": 404}) is None
