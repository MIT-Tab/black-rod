import json
from datetime import date
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory

from core.models import School, Debater, Team, Tournament
from core.models.results.speaker import SpeakerResult
from core.models.results.team import TeamResult
from core.models.standings.qual import QUAL
from core.views import results_import_views as riv


pytestmark = pytest.mark.django_db


def make_request(method="get", path="/wizard/?tournament=1"):
    factory = RequestFactory()
    request = getattr(factory, method.lower())(path)
    request.session = {}
    request.user = SimpleNamespace(has_perms=lambda perms: True, is_authenticated=True)
    return request


def test_dispatch_clears_stale_tournament_session_data(monkeypatch):
    request = make_request(path="/wizard/?tournament=2")
    request.session.update(
        {
            "tournament_id": 1,
            "tournament_api_url": "https://old/",
            "tournament_debater_mapping": {"1": 11},
            "other": "keep",
        }
    )

    def fake_super(self, request, *args, **kwargs):  # pylint: disable=unused-argument
        return HttpResponse("ok")

    monkeypatch.setattr(riv.SessionWizardView, "dispatch", fake_super)

    view = riv.TournamentDataEntryWizardView()
    view.request = request
    response = view.dispatch(request)

    assert response.content == b"ok"
    assert request.session == {"other": "keep"}


def test_get_form_initial_uses_database_seed_for_results():
    school = School.objects.create(name="Test School", included_in_oty=True)
    tournament = Tournament.objects.create(
        name="Invitational",
        host=school,
        date=date(2024, 1, 1),
        season=settings.CURRENT_SEASON,
        toty=True,
        soty=True,
        noty=True,
        num_teams=10,
    )

    varsity_one = Debater.objects.create(first_name="Var", last_name="One", school=school)
    varsity_two = Debater.objects.create(first_name="Var", last_name="Two", school=school)
    novice_one = Debater.objects.create(
        first_name="Nov", last_name="One", school=school, status=Debater.NOVICE
    )
    novice_two = Debater.objects.create(
        first_name="Nov", last_name="Two", school=school, status=Debater.NOVICE
    )

    varsity_team = Team.objects.create(name="Varsity Team")
    varsity_team.debaters.add(var_primary := varsity_one, varsity_two)

    novice_team = Team.objects.create(name="Novice Team")
    novice_team.debaters.add(novice_one, novice_two)

    unplaced_team = Team.objects.create(name="Unplaced")
    unplaced_team.debaters.add(var_primary, novice_one)

    TeamResult.objects.create(
        tournament=tournament,
        team=varsity_team,
        type_of_place=Debater.VARSITY,
        place=1,
        ghost_points=True,
    )
    TeamResult.objects.create(
        tournament=tournament,
        team=novice_team,
        type_of_place=Debater.NOVICE,
        place=1,
    )
    TeamResult.objects.create(
        tournament=tournament,
        team=unplaced_team,
        type_of_place=Debater.VARSITY,
        place=-1,
    )

    SpeakerResult.objects.create(
        tournament=tournament,
        debater=varsity_one,
        type_of_place=Debater.VARSITY,
        place=1,
        tie=True,
    )
    SpeakerResult.objects.create(
        tournament=tournament,
        debater=novice_one,
        type_of_place=Debater.NOVICE,
        place=1,
    )

    view = riv.TournamentDataEntryWizardView()
    request = make_request(path=f"/wizard/?tournament={tournament.id}")
    view.request = request

    varsity_results = view._get_db_initial("2", tournament)
    varsity_speakers = view._get_db_initial("3", tournament)
    novice_results = view._get_db_initial("4", tournament)
    novice_speakers = view._get_db_initial("5", tournament)
    unplaced_results = view._get_db_initial("6", tournament)

    assert varsity_results[0]["debater_one"] == varsity_one
    assert varsity_results[0]["ghost_points"] is True
    assert varsity_speakers[0] == {"speaker": varsity_one, "tie": True}
    assert novice_results[0]["debater_one"] == novice_one
    assert novice_speakers[0]["speaker"] == novice_one
    assert unplaced_results[0]["debater_one"] == var_primary


def test_process_step_creates_entities_from_api(monkeypatch):
    schools_created = []
    debaters_created = []

    class DummyHandler:
        def create_schools_from_data(self, data):
            schools_created.extend(data)

        def create_debaters_from_data(self, data):
            debaters_created.extend(data)

    monkeypatch.setattr(riv.TournamentDataEntryWizardView, "has_api_data", lambda self: True)
    monkeypatch.setattr(riv.SessionWizardView, "process_step", lambda self, form: "ok")

    view = riv.TournamentDataEntryWizardView()
    view.request = make_request(method="post")
    view.steps = SimpleNamespace(current="0")
    view._api_handler = DummyHandler()

    form = SimpleNamespace(
        cleaned_data=[
            {"name": "New School", "included_in_oty": False},
            {"name": ""},
        ]
    )

    assert view.process_step(form) == "ok"
    assert schools_created == [{"name": "New School", "included_in_oty": False}]

    view.steps.current = "1"
    school_obj = object()
    form.cleaned_data = [
        {"first_name": "Deb", "last_name": "Ater", "school": school_obj, "tournament_id": 10},
        {"first_name": "", "last_name": "", "school": None},
    ]

    assert view.process_step(form) == "ok"
    assert debaters_created == [
        {"first_name": "Deb", "last_name": "Ater", "school": school_obj, "tournament_id": 10}
    ]


def test_get_form_prefills_api_initial(monkeypatch):
    monkeypatch.setattr(riv.TournamentDataEntryWizardView, "has_api_data", lambda self: True)

    class FakeForm:
        form_kwargs = {}

        def __init__(self, initial=None, prefix="0", **kwargs):
            self.initial = initial
            self.prefix = prefix
            if kwargs:
                self.form_kwargs = kwargs

    def fake_super_get_form(self, step=None, data=None, files=None):
        return FakeForm(initial=None, prefix="0")

    monkeypatch.setattr(riv.SessionWizardView, "get_form", fake_super_get_form)

    view = riv.TournamentDataEntryWizardView()
    view.request = make_request()
    view._api_handler = SimpleNamespace(get_new_schools_from_api=lambda: [{"name": "API School", "included_in_oty": True}])

    form = view.get_form(step="0")

    assert isinstance(form, FakeForm)
    assert form.initial == [{"name": "API School", "included_in_oty": True}]


def test_update_rankings_noop_for_other_season(monkeypatch):
    view = riv.TournamentDataEntryWizardView()
    request = make_request(path="/wizard/?tournament=1")
    view.request = request

    team = Team.objects.create(name="Skip Team")
    team.debaters.add(
        Debater.objects.create(first_name="Skip", last_name="One", school=School.objects.create(name="S")),
        Debater.objects.create(first_name="Skip", last_name="Two", school=School.objects.create(name="T")),
    )

    called = {"toty": False}

    def fail(*args, **kwargs):  # pylint: disable=unused-argument
        called["toty"] = True

    monkeypatch.setattr(riv, "update_toty", fail)
    monkeypatch.setattr(riv, "redo_rankings", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))

    tournament = Tournament.objects.create(
        name="Old",
        host=School.objects.create(name="Host"),
        date=date(2010, 1, 1),
        season="1999",
    )

    view._update_rankings(tournament, [team], [], [])

    assert called["toty"] is False


def test_done_rebuilds_results_and_triggers_rankings(monkeypatch):
    school = School.objects.create(name="Primary", included_in_oty=True)
    tournament = Tournament.objects.create(
        name="Major",
        host=school,
        date=date(2024, 2, 1),
        season=settings.CURRENT_SEASON,
        toty=True,
        soty=True,
        noty=True,
        num_teams=16,
    )

    varsity_one = Debater.objects.create(first_name="Var", last_name="One", school=school)
    varsity_two = Debater.objects.create(first_name="Var", last_name="Two", school=school)
    novice_one = Debater.objects.create(
        first_name="Nov", last_name="One", school=school, status=Debater.NOVICE
    )
    novice_two = Debater.objects.create(
        first_name="Nov", last_name="Two", school=school, status=Debater.NOVICE
    )

    varsity_team = Team.objects.create(name="Varsity")
    varsity_team.debaters.add(var_primary := varsity_one, varsity_two)
    novice_team = Team.objects.create(name="Novice")
    novice_team.debaters.add(novice_one, novice_two)
    unplaced_team = Team.objects.create(name="Unplaced")
    unplaced_team.debaters.add(var_primary, novice_one)

    TeamResult.objects.create(
        tournament=tournament,
        team=varsity_team,
        type_of_place=Debater.VARSITY,
        place=4,
    )
    SpeakerResult.objects.create(
        tournament=tournament,
        debater=varsity_one,
        type_of_place=Debater.VARSITY,
        place=3,
    )
    QUAL.objects.create(season=tournament.season, debater=varsity_one, qual_type=QUAL.POINTS, tournament=tournament)

    team_calls = {"toty": [], "qual": [], "online": []}
    speaker_calls = {"soty": [], "noty": []}
    redo_calls = []
    cleared = {"done": False}

    def fake_team_lookup(debater_one, debater_two):
        pair = {debater_one.id, debater_two.id}
        if pair == {varsity_one.id, varsity_two.id}:
            return varsity_team
        if pair == {novice_one.id, novice_two.id}:
            return novice_team
        return unplaced_team

    monkeypatch.setattr(riv, "get_or_create_team_for_debaters", fake_team_lookup)

    monkeypatch.setattr(riv, "update_toty", lambda team, season=settings.CURRENT_SEASON: team_calls["toty"].append(team))
    monkeypatch.setattr(riv, "update_qual_points", lambda team, season=settings.CURRENT_SEASON: team_calls["qual"].append(team))
    monkeypatch.setattr(riv, "update_online_quals", lambda team, season=settings.CURRENT_SEASON: team_calls["online"].append(team))
    monkeypatch.setattr(riv, "update_soty", lambda debater, season=settings.CURRENT_SEASON: speaker_calls["soty"].append(debater))
    monkeypatch.setattr(riv, "update_noty", lambda debater, season=settings.CURRENT_SEASON: speaker_calls["noty"].append(debater))
    monkeypatch.setattr(riv, "redo_rankings", lambda qs, season, cache_type: redo_calls.append(cache_type))

    original_clear = riv.APIDataHandler.clear_tournament_session_data

    def spy_clear(request=None):  # pylint: disable=unused-argument
        cleared["done"] = True
        return original_clear(request)

    monkeypatch.setattr(riv.APIDataHandler, "clear_tournament_session_data", staticmethod(spy_clear))

    request = make_request(method="post", path=f"/wizard/?tournament={tournament.id}")
    view = riv.TournamentDataEntryWizardView()
    view.request = request
    view._tournament = tournament

    form_dict = {
        "2": SimpleNamespace(cleaned_data=[{"debater_one": varsity_one, "debater_two": varsity_two, "ghost_points": True, "ORDER": 1}]),
        "4": SimpleNamespace(cleaned_data=[{"debater_one": novice_one, "debater_two": novice_two, "ORDER": 1}]),
        "6": SimpleNamespace(cleaned_data=[{"debater_one": varsity_one, "debater_two": novice_one}]),
        "3": SimpleNamespace(cleaned_data=[{"speaker": varsity_one, "tie": True, "ORDER": 1}]),
        "5": SimpleNamespace(cleaned_data=[{"speaker": novice_one, "tie": False, "ORDER": 1}]),
    }

    response = view.done([], form_dict)

    assert response.status_code == 302
    assert TeamResult.objects.filter(tournament=tournament).count() == 3
    assert SpeakerResult.objects.filter(tournament=tournament).count() == 2
    assert QUAL.objects.filter(tournament=tournament).count() == 0

    assert set(team_calls["toty"]) == {varsity_team, novice_team, unplaced_team}
    assert set(team_calls["qual"]) == {varsity_team, novice_team, unplaced_team}
    assert set(team_calls["online"]) == {varsity_team, novice_team, unplaced_team}
    assert set(speaker_calls["soty"]) == {varsity_one}
    assert set(speaker_calls["noty"]) == {novice_one}
    assert {"toty", "soty", "noty", "coty", "online_quals"}.issubset(set(redo_calls))
    assert cleared["done"] is True


def test_get_new_team_form_renders_requested_form(monkeypatch):
    def fake_render(template, context):  # pylint: disable=unused-argument
        return f"rendered-{context['form'].prefix}-{context['place_number']}"

    monkeypatch.setattr(riv, "render_to_string", fake_render)

    request = make_request(path="/ajax/?form_index=2&form_type=team&has_ghost_points=1")

    response = riv.get_new_team_form(request)

    assert response.status_code == 200
    payload = json.loads(response.content)
    assert payload["html"] == "rendered-2-2-3"


def test_get_new_team_form_rejects_non_get():
    request = RequestFactory().post("/ajax/")

    response = riv.get_new_team_form(request)

    assert response.status_code == 400
    assert json.loads(response.content)["error"] == "Invalid request"


def test_get_new_team_form_sets_order_for_speaker(monkeypatch):
    def fake_render(template, context):  # pylint: disable=unused-argument
        return context["form"].initial

    monkeypatch.setattr(riv, "render_to_string", fake_render)

    request = make_request(path="/ajax/?form_index=1&form_type=speaker")

    response = riv.get_new_team_form(request)

    assert response.status_code == 200
    payload = json.loads(response.content)
    assert payload["html"] == {"ORDER": 2}
