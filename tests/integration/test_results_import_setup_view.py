from datetime import date
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.test import RequestFactory

from core.models import School, Tournament
from core.views import results_import_views as riv


pytestmark = pytest.mark.django_db


def make_tournament():
    school = School.objects.create(name="Setup School", included_in_oty=True)
    return Tournament.objects.create(
        name="Setup Tournament",
        manual_name="Setup Tournament",
        host=school,
        date=date(2024, 1, 1),
        season=settings.CURRENT_SEASON,
    )


def setup_view(view, request):
    request.user = SimpleNamespace(is_authenticated=True, has_perms=lambda perms: True)
    view.setup(request)
    return view


def test_setup_post_redirects_without_api_url_when_no_categories():
    tournament = make_tournament()
    request = RequestFactory().post(
        f"/core/tournaments/data_entry/setup?tournament={tournament.id}",
        data={"tournament": tournament.id},
    )
    view = setup_view(riv.TournamentDataEntrySetupView(), request)

    response = view.post(request)

    assert response.status_code == 302
    assert f"tournament={tournament.id}" in response["Location"]
    assert "api_url=" not in response["Location"]


def test_setup_post_redirects_with_selected_api_categories(monkeypatch):
    tournament = make_tournament()

    class FakeHandler:
        def __init__(self, request):  # pylint: disable=unused-argument
            pass

        def set_api_url(self, url):  # pylint: disable=unused-argument
            return None

        def validate_api_connection(self):
            return True, None

    monkeypatch.setattr(riv, "APIDataHandler", FakeHandler)

    request = RequestFactory().post(
        f"/core/tournaments/data_entry/setup?tournament={tournament.id}",
        data={
            "tournament": tournament.id,
            "api_url": "https://api.example",
            "import_varsity_teams": "on",
            "import_counts": "on",
        },
    )
    view = setup_view(riv.TournamentDataEntrySetupView(), request)

    response = view.post(request)

    assert response.status_code == 302
    assert f"tournament={tournament.id}" in response["Location"]
    assert "api_url=https%3A%2F%2Fapi.example" in response["Location"]
    assert "import_varsity_teams=1" in response["Location"]
    assert "import_counts=1" in response["Location"]


def test_setup_post_surfaces_api_errors(monkeypatch):
    tournament = make_tournament()

    class FakeHandler:
        def __init__(self, request):  # pylint: disable=unused-argument
            pass

        def set_api_url(self, url):  # pylint: disable=unused-argument
            return None

        def validate_api_connection(self):
            return False, "boom"

    monkeypatch.setattr(riv, "APIDataHandler", FakeHandler)

    request = RequestFactory().post(
        f"/core/tournaments/data_entry/setup?tournament={tournament.id}",
        data={
            "tournament": tournament.id,
            "api_url": "https://api.example",
            "import_varsity_teams": "on",
        },
    )
    view = setup_view(riv.TournamentDataEntrySetupView(), request)

    response = view.post(request)

    assert response.status_code == 200
    assert b"Could not connect to the Mit-Tab tournament import source" in response.content
    assert b"boom" not in response.content
