from datetime import date
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from django.test import RequestFactory

from api.views import _collect_coty_replay_standings, _coty_breakdown_for_school
from core import live_results
from core.forms import TournamentAutoQualForm
from core.models import COTY, QUAL, Debater, QualPoints, School, Team, TeamResult, Tournament, TournamentAutoQual
from core.templatetags.tags import qual_contribution
from core.utils import rankings, merge
from core.views.results_import_views import RESULT_FORMSET_CONFIGS, TournamentDataEntryView
from core.views.tournament_views import TournamentListView


pytestmark = pytest.mark.django_db
BP_TYPES = [Tournament.NAUDC, Tournament.WORLDS, Tournament.USUDC, Tournament.NORTHAMS, Tournament.YALE, Tournament.BRANDEIS]


@pytest.fixture
def participants(settings):
    settings.CURRENT_SEASON = "2026"
    cache.clear()
    school = School.objects.create(name="Autoqual University", included_in_oty=True)
    debater = Debater.objects.create(first_name="Individual", last_name="Recipient", school=school)
    return school, debater


def tournament_for(school, kind, season="2026", month=9):
    return Tournament.objects.create(
        host=school, season=season, date=date(int(season), month, 1), qual_type=kind, num_teams=32
    )


def coty_points(school, season="2026"):
    return sum(COTY.objects.filter(school=school, season=season).values_list("points", flat=True))


@pytest.mark.parametrize("rebuild", [rankings.rebuild_coty_related_rankings, rankings.rebuild_import_qualifications])
@pytest.mark.parametrize("kind", BP_TYPES)
@pytest.mark.parametrize("season,bonus", [("2025", 6), ("2026", 0), ("2027", 0)])
def test_individual_bp_qualification_and_season_bonus(participants, settings, kind, season, bonus, rebuild):
    settings.CURRENT_SEASON = season
    school, debater = participants
    tournament = tournament_for(school, kind, season)
    TournamentAutoQual.objects.create(tournament=tournament, debater=debater)

    rebuild(season)
    rebuild(season)

    assert QUAL.objects.filter(debater=debater, season=season, qual_type=kind).count() == 1
    assert coty_points(school, season) == bonus
    assert not Team.objects.exists()
    assert not TeamResult.objects.exists()
    breakdown = _coty_breakdown_for_school(school, season, None)
    assert breakdown[0]["qualled"] is True
    assert breakdown[0]["contribution"] == bonus
    display_points = rankings.get_qualled_debaters(school, season)[0]
    assert qual_contribution(display_points, season) == bonus
    assert tournament in TournamentListView().get_queryset()


@pytest.mark.parametrize("kind", [Tournament.NAUDC, Tournament.WORLDS, Tournament.NORTHAMS, Tournament.YALE, Tournament.BRANDEIS])
def test_placement_bp_autoquals_use_same_bonus_rule(participants, kind):
    school, debater = participants
    tournament = tournament_for(school, kind)
    team = Team.objects.create()
    team.debaters.add(debater)
    TeamResult.objects.create(tournament=tournament, team=team, place=1, type_of_place=Debater.VARSITY)
    rankings.update_qual_points(team, "2026")
    assert QUAL.objects.filter(debater=debater, qual_type=kind).exists()
    assert coty_points(school) == 0


@pytest.mark.parametrize("rebuild", [rankings.rebuild_coty_related_rankings, rankings.rebuild_import_qualifications])
@pytest.mark.parametrize("bp_first", [True, False])
def test_bp_qual_then_points_qual_awards_bonus_once(participants, bp_first, rebuild):
    school, debater = participants
    bp = tournament_for(school, Tournament.YALE)
    regular = tournament_for(school, Tournament.POINTS, month=10)
    team = Team.objects.create()
    team.debaters.add(debater)
    if bp_first:
        TournamentAutoQual.objects.create(tournament=bp, debater=debater)
    result = TeamResult.objects.create(tournament=regular, team=team, place=4, type_of_place=Debater.VARSITY)
    rankings.update_qual_points(team, "2026")
    assert coty_points(school) == regular.get_qual_points(4)
    result.place = 1
    result.save()
    rankings.update_qual_points(team, "2026")
    if not bp_first:
        TournamentAutoQual.objects.create(tournament=bp, debater=debater)
    rebuild("2026")
    assert QUAL.objects.filter(debater=debater, qual_type=QUAL.POINTS).exists()
    assert QUAL.objects.filter(debater=debater, qual_type=QUAL.YALE).exists()
    assert coty_points(school) == regular.get_qual_points(1) + 6
    # Removing the ordinary qualification must remove the bonus while retaining the BP qualification.
    result.place = 4
    result.save()
    rebuild("2026")
    assert QUAL.objects.filter(debater=debater, qual_type=QUAL.YALE).exists()
    assert not QUAL.objects.filter(debater=debater, qual_type=QUAL.POINTS).exists()
    assert coty_points(school) == regular.get_qual_points(4)


def test_later_non_bp_autoqual_earns_bonus_and_survives_merge(participants):
    school, debater = participants
    bp = tournament_for(school, Tournament.USUDC)
    non_bp = tournament_for(school, Tournament.GENDER_MINORITY, month=10)
    TournamentAutoQual.objects.create(tournament=bp, debater=debater)
    other = Debater.objects.create(first_name="Other", school=school)
    TournamentAutoQual.objects.create(tournament=non_bp, debater=other)
    rankings.rebuild_coty_related_rankings("2026")
    assert coty_points(school) == 6
    merge.merge_debaters(debater, other)
    assert debater.tournament_autoquals.count() == 2
    rankings.rebuild_coty_related_rankings("2026")
    assert coty_points(school) == 6
    assert set(debater.quals.values_list("qual_type", flat=True)) == {QUAL.USUDC, QUAL.GENDER_MINORITY}
    rankings.update_soty(debater, "2026")
    assert Debater.objects.filter(pk=debater.pk).exists()


def entry_request(tournament, recipients):
    data = {"tournament": tournament.pk, "autoqual_debaters": [debater.pk for debater in recipients]}
    for config in RESULT_FORMSET_CONFIGS:
        data.update({f"{config.key}-TOTAL_FORMS": "0", f"{config.key}-INITIAL_FORMS": "0"})
    request = RequestFactory().post(f"/tournaments/dataentry?tournament={tournament.pk}", data)
    request.user = SimpleNamespace(has_perms=lambda perms: True, is_authenticated=True)
    request.session = {}
    return request


def test_normal_data_entry_saves_and_removes_individual_recipients(participants):
    school, debater = participants
    tournament = tournament_for(school, Tournament.USUDC)
    response = TournamentDataEntryView.as_view()(entry_request(tournament, [debater]))
    assert response.status_code == 302
    assert TournamentAutoQual.objects.filter(tournament=tournament, debater=debater).exists()
    assert QUAL.objects.filter(tournament=tournament, debater=debater).exists()
    assert list(TournamentAutoQualForm(tournament=tournament).initial["autoqual_debaters"]) == [debater.pk]
    assert not Team.objects.exists()
    response = TournamentDataEntryView.as_view()(entry_request(tournament, []))
    assert response.status_code == 302
    assert not tournament.autoqual_recipients.exists()
    assert not debater.quals.exists()


def test_removing_one_manual_source_keeps_other_source_of_same_type(participants):
    school, debater = participants
    first = tournament_for(school, Tournament.GENDER_MINORITY)
    second = tournament_for(school, Tournament.GENDER_MINORITY, month=10)
    for tournament in (first, second):
        response = TournamentDataEntryView.as_view()(entry_request(tournament, [debater]))
        assert response.status_code == 302
    assert coty_points(school) == 6
    response = TournamentDataEntryView.as_view()(entry_request(first, []))
    assert response.status_code == 302
    assert QUAL.objects.get(debater=debater).tournament == second
    assert coty_points(school) == 6
    TournamentDataEntryView.as_view()(entry_request(second, []))
    assert not debater.quals.exists()
    assert coty_points(school) == 0


def test_invalid_individual_entry_is_rejected(participants):
    school, debater = participants
    regular = tournament_for(school, Tournament.POINTS)
    form = TournamentAutoQualForm({"autoqual_debaters": [debater.pk]}, tournament=regular)
    assert not form.is_valid()
    assert "autoqual_debaters" in form.errors
    bp = tournament_for(school, Tournament.YALE)
    form = TournamentAutoQualForm({"autoqual_debaters": [99999]}, tournament=bp)
    assert not form.is_valid()
    assert not TournamentAutoQual.objects.exists()


def test_replay_defers_bonus_until_non_bp_qualification(participants):
    school, debater = participants
    bp = tournament_for(school, Tournament.YALE)
    regular = tournament_for(school, Tournament.POINTS, month=10)
    TournamentAutoQual.objects.create(tournament=bp, debater=debater)
    team = Team.objects.create()
    team.debaters.add(debater)
    TeamResult.objects.create(tournament=regular, team=team, place=1, type_of_place=Debater.VARSITY)
    rows, dates = _collect_coty_replay_standings("2026", None)
    assert dates == [bp.date.isoformat(), regular.date.isoformat()]
    markers = rows[0]["all_markers"]
    qualification = next(marker for marker in markers if marker["marker_type"] == "autoqual")
    assert qualification["points"] == 0
    assert qualification["place"] is None
    assert qualification["result_id"] is None
    bonus = next(marker for marker in markers if marker["marker_type"] == "qual_bonus")
    assert bonus["earned_on"] == regular.date.isoformat()
    assert bonus["qualification"]["type"] == QUAL.POINTS
    assert rows[0]["points"] == regular.get_qual_points(1) + 6


def test_live_bp_qualified_debater_can_later_earn_bonus(participants):
    school, debater = participants
    QUAL.objects.create(debater=debater, season="2026", qual_type=QUAL.YALE)
    QualPoints.objects.create(debater=debater, season="2026", points=10)
    coty = live_results._base_coty("2026")
    entry = coty["debaters"][f"debater:{debater.pk}"]
    assert entry["qualled"] is True
    assert entry["bonus_eligible"] is False
    person = {"key": f"debater:{debater.pk}", "school_key": f"school:{school.pk}", "school_eligible": True}
    live_results._apply_coty({"flags": live_results._type_flags(Tournament.POINTS, "2026"), "type_code": Tournament.POINTS, "season": "2026", "coty": coty}, [person], 4, 3.5)
    rows = live_results._finalize_coty(coty)
    assert rows[0]["points"] == 19.5
    assert rows[0]["debaters"][0]["contribution_live"] is True
    assert rows[0]["debaters"][0]["qualled_live"] is False


def test_migration_recalculates_only_new_seasons(participants):
    from importlib import import_module
    from django.apps import apps
    from django.db import connection

    school, debater = participants
    for season in ("2025", "2026"):
        QUAL.objects.create(debater=debater, season=season, qual_type=QUAL.YALE)
        QualPoints.objects.create(debater=debater, season=season, points=10)
        COTY.objects.create(school=school, season=season, points=16)
    migration = import_module("core.migrations.0074_individual_autoquals")
    migration.rebuild_coty_bonuses(apps, connection.schema_editor())
    assert coty_points(school, "2025") == 16
    assert coty_points(school, "2026") == 10


def test_entry_form_and_detail_show_individual_recipients(participants, client):
    from django.urls import reverse
    from core.models import User

    school, debater = participants
    tournament = tournament_for(school, Tournament.USUDC)
    TournamentDataEntryView.as_view()(entry_request(tournament, [debater]))
    user = User.objects.create_superuser(username="autoqual-editor", password="test-password", email="editor@example.org")
    client.force_login(user)
    response = client.get(reverse("core:tournament_dataentry"), {"tournament": tournament.pk})
    assert response.status_code == 200
    assert b"Individual autoqual recipients" in response.content
    assert b"No team or placement is recorded" in response.content
    assert debater.name.encode() in response.content
    response = client.get(tournament.get_absolute_url())
    assert response.status_code == 200
    assert b"Individual Autoqual Recipients" in response.content
    assert debater.name.encode() in response.content
