from datetime import date
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from core.models import School, Tournament, Debater, Team
from core.models.results.speaker import SpeakerResult
from core.models.results.team import TeamResult
from core.models.round import Round
from core.views import tournament_views as tv


pytestmark = pytest.mark.django_db


@pytest.fixture
def school():
    return School.objects.create(name="Test School", included_in_oty=True)


def test_tournament_filter_defaults_to_current_season(school):
    current = Tournament.objects.create(
        name="Current",
        manual_name="Current",
        host=school,
        date=date(2024, 1, 1),
        season=settings.CURRENT_SEASON,
    )
    Tournament.objects.create(
        name="Other",
        manual_name="Other",
        host=school,
        date=date(2023, 1, 1),
        season="2000",
    )

    filt = tv.TournamentFilter(queryset=Tournament.objects.all())

    ids = list(filt.qs.values_list("id", flat=True))
    assert ids == [current.id]


def test_tournament_list_view_filters_to_tournaments_with_results(monkeypatch, school):
    with_results = Tournament.objects.create(
        name="Resulted",
        manual_name="Resulted",
        host=school,
        date=date(2024, 2, 1),
        season=settings.CURRENT_SEASON,
    )
    TeamResult.objects.create(
        tournament=with_results,
        team=Team.objects.create(name="T"),
        type_of_place=Debater.VARSITY,
        place=1,
    )

    without_results = Tournament.objects.create(
        name="Empty",
        manual_name="Empty",
        host=school,
        date=date(2024, 3, 1),
        season=settings.CURRENT_SEASON,
    )

    monkeypatch.setattr(
        tv.CustomListView,
        "get_queryset",
        lambda self, *args, **kwargs: Tournament.objects.all(),
    )

    request = RequestFactory().get("/tournaments/")
    request.user = SimpleNamespace(is_authenticated=True, has_perms=lambda perms: True)

    view = tv.TournamentListView()
    view.setup(request)

    qs = view.get_queryset()

    assert list(qs) == [with_results]
    assert without_results not in qs


def test_tournament_detail_view_builds_context(monkeypatch, school):
    tournament = Tournament.objects.create(
        name="Contextual",
        manual_name="Contextual",
        host=school,
        date=date(2024, 4, 1),
        season=settings.CURRENT_SEASON,
    )

    varsity_team = Team.objects.create(name="Varsity Team")
    novice_team = Team.objects.create(name="Novice Team")
    overlap_team = Team.objects.create(name="Overlap Team")

    TeamResult.objects.create(
        tournament=tournament,
        team=varsity_team,
        type_of_place=Debater.VARSITY,
        place=1,
    )
    TeamResult.objects.create(
        tournament=tournament,
        team=novice_team,
        type_of_place=Debater.NOVICE,
        place=1,
    )
    TeamResult.objects.create(
        tournament=tournament,
        team=overlap_team,
        type_of_place=Debater.VARSITY,
        place=2,
    )

    varsity_debater = Debater.objects.create(first_name="Var", last_name="One", school=school)
    novice_debater = Debater.objects.create(first_name="Nov", last_name="One", school=school, status=Debater.NOVICE)

    SpeakerResult.objects.create(
        tournament=tournament,
        debater=varsity_debater,
        type_of_place=Debater.VARSITY,
        place=1,
        tie=False,
    )
    SpeakerResult.objects.create(
        tournament=tournament,
        debater=varsity_debater,
        type_of_place=Debater.VARSITY,
        place=2,
        tie=True,
    )
    SpeakerResult.objects.create(
        tournament=tournament,
        debater=novice_debater,
        type_of_place=Debater.NOVICE,
        place=1,
        tie=True,
    )

    Round.objects.create(gov=varsity_team, opp=overlap_team, tournament=tournament)

    monkeypatch.setattr(tv, "get_tab_card_data", lambda team, _t: f"tab-{team.id}")

    request = RequestFactory().get("/")
    request.user = SimpleNamespace(is_authenticated=True, has_perms=lambda perms: True)
    view = tv.TournamentDetailView()
    view.setup(request, pk=tournament.pk)
    view.object = tournament

    context = view.get_context_data(object=tournament)

    assert [r.team for r in context["varsity_team_results"]] == [varsity_team, overlap_team]
    assert [r.team for r in context["novice_team_results"]] == [novice_team]
    assert context["varsity_speaker_results"][0].tie is True
    assert context["novice_speaker_results"][0].place == 0  # tie adjusts place
    assert context["tab_cards_available"] is True
    assert len(context["teams"]) == 2
    assert all(label.startswith("tab-") for _, label in context["teams"])


def test_tournament_create_view_handles_api_failure(monkeypatch, school):
    called = {"cleared": False, "errors": []}

    class FakeHandler:
        def __init__(self, request):
            self.request = request

        def set_api_url(self, url):  # pylint: disable=unused-argument
            return None

        def validate_api_connection(self):
            return False, "boom"

        def set_tournament_id(self, tournament_id):  # pylint: disable=unused-argument
            called["set_tid"] = True

    monkeypatch.setattr(tv, "APIDataHandler", FakeHandler)

    def fake_error(request, message):  # pylint: disable=unused-argument
        called["errors"].append(message)

    monkeypatch.setattr(tv.messages, "error", fake_error)

    invalid_called = {"count": 0}

    def fake_form_invalid(self, form):  # pylint: disable=unused-argument
        invalid_called["count"] += 1
        return HttpResponse("invalid")

    monkeypatch.setattr(tv.CustomCreateView, "form_invalid", fake_form_invalid)

    view = tv.TournamentCreateView()
    request = RequestFactory().post("/create/")
    request.user = SimpleNamespace(is_authenticated=True, has_perms=lambda perms: True)
    request.session = {}
    view.setup(request)

    form = SimpleNamespace(
        cleaned_data={"api_url": "https://api.example"},
        add_error=lambda field, error: called.setdefault("form_errors", []).append((field, error)),
    )

    response = view.form_valid(form)

    assert response.status_code == 200
    assert called["cleared"] is True
    assert called["errors"] == ["API Error: boom"]
    assert ("api_url", "API Error: boom") in called["form_errors"]
    assert invalid_called["count"] == 1


def test_tournament_create_view_redirects_on_success(monkeypatch, school):
    tournament = Tournament.objects.create(
        name="Fresh",
        manual_name="Fresh",
        host=school,
        date=date(2024, 5, 1),
        season=settings.CURRENT_SEASON,
    )

    class FakeHandler:
        def __init__(self, request):
            self.request = request


        def set_api_url(self, url):  # pylint: disable=unused-argument
            return None

        def validate_api_connection(self):
            return True, None

        def set_tournament_id(self, tournament_id):
            self.request.session["tid"] = tournament_id

    monkeypatch.setattr(tv, "APIDataHandler", FakeHandler)

    def fake_form_valid(self, form):  # pylint: disable=unused-argument
        self.object = tournament
        return HttpResponse("ok")

    monkeypatch.setattr(tv.CustomCreateView, "form_valid", fake_form_valid)

    view = tv.TournamentCreateView()
    request = RequestFactory().post("/create/")
    request.user = SimpleNamespace(is_authenticated=True, has_perms=lambda perms: True)
    request.session = {}
    view.setup(request)

    form = SimpleNamespace(cleaned_data={"api_url": "https://api.example"})

    response = view.form_valid(form)

    assert response.status_code == 302
    # Check that both tournament and api_url are in the redirect URL
    assert f"tournament={tournament.id}" in response["Location"]
    assert "api_url=https%3A%2F%2Fapi.example" in response["Location"]
    assert response["Location"].startswith(reverse('core:tournament_dataentry'))


def test_all_tournament_autocomplete_filters_query(school):
    match = Tournament.objects.create(
        name="Match",
        manual_name="Match",
        host=school,
        date=date(2024, 6, 1),
        season=settings.CURRENT_SEASON,
    )
    Tournament.objects.create(
        name="Other",
        manual_name="Other",
        host=school,
        date=date(2024, 7, 1),
        season=settings.CURRENT_SEASON,
    )

    view = tv.AllTournamentAutocomplete()
    request = RequestFactory().get("/ac/?q=Mat")
    view.setup(request)
    view.q = "Mat"

    results = view.get_queryset()
    assert list(results) == [match]
    assert view.get_result_label(match).startswith("<")


def test_tournament_autocomplete_only_unentered(school):
    included = Tournament.objects.create(
        name="Included",
        manual_name="Included",
        host=school,
        date=date(2024, 8, 1),
        season=settings.CURRENT_SEASON,
    )
    excluded = Tournament.objects.create(
        name="Excluded",
        manual_name="Excluded",
        host=school,
        date=date(2024, 9, 1),
        season=settings.CURRENT_SEASON,
    )
    TeamResult.objects.create(
        tournament=excluded,
        team=Team.objects.create(name="Excluded Team"),
        type_of_place=Debater.VARSITY,
        place=1,
    )

    view = tv.TournamentAutocomplete()
    request = RequestFactory().get("/ac/")
    view.setup(request)
    view.q = None

    qs = list(view.get_queryset())
    assert included in qs
    assert excluded not in qs


def test_schedule_view_groups_by_month(school):
    year = int(settings.CURRENT_SEASON)
    Tournament.objects.create(
        name="Alpha",
        manual_name="Alpha",
        host=school,
        date=date(year, 1, 5),
        season=settings.CURRENT_SEASON,
        qual_type=0,
    )
    Tournament.objects.create(
        name="Beta",
        manual_name="Beta",
        host=school,
        date=date(year, 1, 12),
        season=settings.CURRENT_SEASON,
        qual_type=1,
    )
    Tournament.objects.create(
        name="Gamma",
        manual_name="Gamma",
        host=school,
        date=date(year, 2, 2),
        season=settings.CURRENT_SEASON,
        qual_type=2,
    )

    request = RequestFactory().get(f"/schedule/?season={settings.CURRENT_SEASON}")
    request.user = SimpleNamespace(is_authenticated=True, has_perms=lambda perms: True)

    view = tv.ScheduleView()
    view.setup(request)
    context = view.get_context_data()

    assert context["current_season"] == settings.CURRENT_SEASON
    jan_entry = next(item for item in context["tournaments"] if item["month"] == 1)
    jan_names = [t.name for week in jan_entry["weeks"] for t in week["tournaments"]]
    assert "Alpha" in jan_names
    assert "Beta" in jan_names
    assert any(entry["month"] == 2 for entry in context["tournaments"])
