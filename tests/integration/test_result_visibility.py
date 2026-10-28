"""Per-member result hiding across admin editing and public profiles."""

from datetime import date
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse

from core.admin import TeamResultAdminForm
from core.models import Debater, School, SpeakerResult, Team, TeamResult, Tournament
from core.models.round import Round
from core.models.standings.soty import SOTY
from core.models.standings.toty import TOTY
from core.utils.merge import merge_debaters
from core.utils.rankings import update_soty, update_toty
from core.views import results_import_views as imports


pytestmark = pytest.mark.django_db


@pytest.fixture
def competition():
    school = School.objects.create(name="Privacy University")
    subject = Debater.objects.create(first_name="Hidden", last_name="Member", school=school)
    partner = Debater.objects.create(first_name="Visible", last_name="Partner", school=school)
    team = Team.objects.create(name="Distinctive Team Name")
    team.debaters.add(subject, partner)
    tournament = Tournament.objects.create(
        manual_name="Private Profile Tournament", host=school, date=date(2024, 2, 1),
        season="2024", num_teams=32,
    )
    result = TeamResult.objects.create(team=team, tournament=tournament, place=1)
    opponent = Team.objects.create(name="Opposing Team")
    Round.objects.create(tournament=tournament, gov=team, opp=opponent, round_number=1)
    return SimpleNamespace(
        school=school, subject=subject, partner=partner, team=team,
        tournament=tournament, result=result,
    )


def profile_url(debater, api=False):
    return reverse(f"{'api' if api else 'core'}:debater_detail", args=[debater.pk])


def test_results_are_public_by_default(client, competition):
    data = competition
    for member in (data.subject, data.partner):
        response = client.get(profile_url(member))
        assert response.context["results"][0]["team"] == data.team
        assert response.context["results"][0]["data"] == [("team", data.result)]
        assert data.team in response.context["teams"]
        payload = client.get(profile_url(member, api=True)).json()
        assert payload["season_summaries"][0]["tournaments"][0]["team"]["id"] == data.team.pk
    assert not data.result.hidden_from_debaters.exists()


@pytest.mark.parametrize("show_all", [False, True])
@pytest.mark.parametrize("logged_in", [False, True])
def test_one_hidden_member_preserves_partner_award_without_pairing(
    client, competition, settings, show_all, logged_in,
):
    data = competition
    settings.ENABLE_DEBATER_PROFILE_TAB_CARDS = True
    settings.ENABLE_DEBATER_PARTNER_PIE_CHART = True
    if logged_in:
        client.force_login(get_user_model().objects.create_user(username="viewer"))
    data.result.hidden_from_debaters.add(data.subject)
    params = {"all": "1"} if show_all else {}

    hidden = client.get(profile_url(data.subject), params)
    assert hidden.context["results"] == []
    assert hidden.context["seasons"] == []
    assert data.tournament.name not in hidden.content.decode()
    visible = client.get(profile_url(data.partner), params)
    row = visible.context["results"][0]
    assert row["data"] == [("team", data.result)]
    assert row["team"] is None
    assert row["tab_card"] is None
    assert visible.context["partner_breakdown"] is None
    assert not visible.context["teams"]
    assert data.team.name not in visible.content.decode()
    assert data.subject.name not in visible.content.decode()
    assert "Varsity Team Award" in visible.content.decode()

    team_profile = client.get(reverse("core:team_detail", args=[data.team.pk]), params)
    assert team_profile.context["team_results"] == []
    assert data.tournament.name not in team_profile.content.decode()
    # Hiding affects public display, not the underlying competition record.
    data.result.refresh_from_db()
    assert data.result.counts_for_points
    assert data.result.place == 1
    assert Round.objects.filter(tournament=data.tournament, gov=data.team).exists()


def test_profile_apis_hide_award_and_redact_nested_team(client, competition):
    data = competition
    data.result.hidden_from_debaters.add(data.subject)
    hidden = client.get(profile_url(data.subject, api=True)).json()
    assert hidden["season_summaries"] == []
    assert hidden["teams"] == []
    response = client.get(profile_url(data.partner, api=True))
    payload = response.json()
    row = payload["season_summaries"][0]["tournaments"][0]
    assert row["team"] is None
    assert row["team_results"][0]["team"] is None
    assert row["team_results"][0]["place"] == 1
    assert row["tab_card"] == []
    assert row["record"] == ""
    assert payload["teams"] == []
    assert data.subject.name not in response.content.decode()
    assert data.team.name not in response.content.decode()
    team = client.get(reverse("api:team_detail", args=[data.team.pk])).json()
    assert team["tournaments"] == []


def test_speaker_visibility_is_independent_and_preserves_other_awards(client, competition):
    data = competition
    data.result.hidden_from_debaters.add(data.subject)
    varsity = SpeakerResult.objects.create(
        debater=data.subject, tournament=data.tournament, place=1, hidden_from_profile=True,
    )
    novice = SpeakerResult.objects.create(
        debater=data.subject, tournament=data.tournament, place=2, type_of_place=Debater.NOVICE,
    )
    response = client.get(profile_url(data.subject), {"all": "1"})
    row = response.context["results"][0]
    assert row["data"] == [("speaker", novice)]
    assert row["team"] is None
    api = client.get(profile_url(data.subject, api=True)).json()
    row = api["season_summaries"][0]["tournaments"][0]
    assert [award["id"] for award in row["speaker_results"]] == [novice.pk]
    assert row["team_results"] == []
    varsity.refresh_from_db()
    assert varsity.counts_for_points


def test_standalone_hidden_speaker_result_does_not_leave_empty_tournament(client, competition):
    data = competition
    data.team.debaters.remove(data.subject)
    result = SpeakerResult.objects.create(
        debater=data.subject, tournament=data.tournament, place=1, hidden_from_profile=True,
    )
    assert client.get(profile_url(data.subject)).context["results"] == []
    assert client.get(profile_url(data.subject, api=True)).json()["season_summaries"] == []
    result.hidden_from_profile = False
    result.save()
    assert client.get(profile_url(data.subject)).context["results"][0]["data"] == [("speaker", result)]


def test_hiding_is_per_result_and_reversible(client, competition):
    data = competition
    data.result.hidden_from_debaters.add(data.subject, data.partner)
    novice = TeamResult.objects.create(
        team=data.team, tournament=data.tournament, place=2, type_of_place=Debater.NOVICE,
    )
    assert client.get(profile_url(data.subject)).context["results"][0]["data"] == [("team", novice)]
    team = client.get(reverse("core:team_detail", args=[data.team.pk]), {"all": "1"})
    assert [row["result"] for row in team.context["team_results"]] == [novice]
    data.result.hidden_from_debaters.clear()
    restored = client.get(profile_url(data.subject)).context["results"][0]
    assert restored["team"] == data.team
    assert ("team", data.result) in restored["data"]
    assert data.team in client.get(profile_url(data.partner)).context["teams"]


def test_profile_standings_keep_totals_without_hidden_markers_or_team(client, competition):
    data = competition
    data.result.hidden_from_debaters.add(data.subject)
    SpeakerResult.objects.create(
        debater=data.subject, tournament=data.tournament, place=1, hidden_from_profile=True,
    )
    SOTY.objects.create(
        debater=data.subject, season="2024", points=10, place=1,
        marker_one=10, tournament_one=data.tournament,
    )
    TOTY.objects.create(
        team=data.team, season="2024", points=12, place=1,
        marker_one=12, tournament_one=data.tournament,
    )
    profile = client.get(profile_url(data.subject, api=True)).json()
    assert profile["standings"]["toty"] == []
    assert profile["standings"]["soty"][0]["points"] == 10
    assert profile["standings"]["soty"][0]["markers"] == []
    team = client.get(reverse("api:team_detail", args=[data.team.pk])).json()
    assert team["toty_history"][0]["points"] == 12
    assert team["toty_history"][0]["markers"] == []


def test_admin_form_limits_visibility_choices_to_team_members(competition):
    data = competition
    outsider = Debater.objects.create(first_name="Outsider", school=data.school)
    form = TeamResultAdminForm(instance=data.result)
    assert set(form.fields["hidden_from_debaters"].queryset) == {data.subject, data.partner}
    form = TeamResultAdminForm(instance=data.result, data={
        "team": data.team.pk, "tournament": data.tournament.pk,
        "type_of_place": TeamResult.VARSITY, "place": 1,
        "hidden_from_debaters": [outsider.pk],
    })
    assert not form.is_valid()
    assert "hidden_from_debaters" in form.errors


def test_admin_can_toggle_individual_team_and_speaker_results(client, competition):
    data = competition
    admin = get_user_model().objects.create_superuser(
        username="visibility-admin", email="admin@example.com", password="test",
    )
    client.force_login(admin)
    url = reverse("admin:core_teamresult_change", args=[data.result.pk])
    response = client.get(url)
    assert response.status_code == 200
    assert data.subject.name in response.content.decode()
    for members in ([data.subject.pk], [data.partner.pk], []):
        response = client.post(url, {
            "team": data.team.pk, "tournament": data.tournament.pk, "place": 1,
            "type_of_place": TeamResult.VARSITY, "counts_for_points": "on",
            "hidden_from_debaters": members, "_save": "Save",
        })
        assert response.status_code == 302
        assert set(data.result.hidden_from_debaters.values_list("pk", flat=True)) == set(members)
    speaker = SpeakerResult.objects.create(debater=data.subject, tournament=data.tournament, place=1)
    url = reverse("admin:core_speakerresult_change", args=[speaker.pk])
    for hidden in (True, False):
        values = {
            "debater": data.subject.pk, "tournament": data.tournament.pk, "place": 1,
            "type_of_place": SpeakerResult.VARSITY, "counts_for_points": "on", "_save": "Save",
        }
        if hidden:
            values["hidden_from_profile"] = "on"
        assert client.post(url, values).status_code == 302
        speaker.refresh_from_db()
        assert speaker.hidden_from_profile == hidden


def test_regular_users_cannot_edit_visibility(client, competition):
    user = get_user_model().objects.create_user(username="ordinary-user")
    client.force_login(user)
    url = reverse("admin:core_teamresult_change", args=[competition.result.pk])
    assert client.post(url, {"hidden_from_debaters": [competition.subject.pk]}).status_code == 302
    assert not competition.result.hidden_from_debaters.exists()
    assert "Manage Team Results" not in client.get(profile_url(competition.subject)).content.decode()


def test_staff_with_view_permission_can_inspect_but_not_change_visibility(client, competition):
    user = get_user_model().objects.create_user(username="read-only-admin", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="view_teamresult"))
    client.force_login(user)
    url = reverse("admin:core_teamresult_change", args=[competition.result.pk])
    assert client.get(url).status_code == 200
    assert client.post(url, {"hidden_from_debaters": [competition.subject.pk]}).status_code == 403
    assert not competition.result.hidden_from_debaters.exists()


def test_tournament_data_entry_preserves_visibility_after_replacing_results(competition, monkeypatch):
    data = competition
    data.result.hidden_from_debaters.add(data.subject)
    SpeakerResult.objects.create(
        debater=data.subject, tournament=data.tournament, place=1, hidden_from_profile=True,
    )
    monkeypatch.setattr(imports, "update_rankings", lambda *args: None)
    monkeypatch.setattr(imports, "reindex_debaters", lambda *args: None)
    formsets = {
        config.key: SimpleNamespace(cleaned_data=[])
        for config in imports.TournamentDataEntryView.result_formsets
    }
    formsets["varsity_teams"].cleaned_data = [{
        "debater_one": data.subject, "debater_two": data.partner, "ORDER": 2,
    }]
    formsets["varsity_speakers"].cleaned_data = [{"speaker": data.subject, "ORDER": 3}]
    imports.TournamentDataEntryView().persist_results(data.tournament, formsets, use_api=False)
    team_result = TeamResult.objects.get(team=data.team, tournament=data.tournament)
    speaker_result = SpeakerResult.objects.get(debater=data.subject, tournament=data.tournament)
    assert team_result.pk != data.result.pk
    assert team_result.place == 2
    assert list(team_result.hidden_from_debaters.all()) == [data.subject]
    assert speaker_result.place == 3
    assert speaker_result.hidden_from_profile


def test_hidden_results_still_count_towards_rankings(competition):
    data = competition
    SpeakerResult.objects.create(debater=data.subject, tournament=data.tournament, place=1)
    update_toty(data.team, season="2024")
    update_soty(data.subject, season="2024")
    team_points = TOTY.objects.get(team=data.team, season="2024").points
    speaker_points = SOTY.objects.get(debater=data.subject, season="2024").points
    assert team_points > 0
    assert speaker_points > 0
    data.result.hidden_from_debaters.add(data.subject)
    data.subject.speaker_results.update(hidden_from_profile=True)
    update_toty(data.team, season="2024")
    update_soty(data.subject, season="2024")
    assert TOTY.objects.get(team=data.team, season="2024").points == team_points
    assert SOTY.objects.get(debater=data.subject, season="2024").points == speaker_points


def test_debater_merge_preserves_visibility(competition):
    data = competition
    replacement = Debater.objects.create(first_name="Replacement", school=data.school)
    data.result.hidden_from_debaters.add(data.subject)
    speaker = SpeakerResult.objects.create(
        debater=data.subject, tournament=data.tournament, place=1, hidden_from_profile=True,
    )
    merge_debaters(replacement, data.subject)
    assert list(data.result.hidden_from_debaters.all()) == [replacement]
    speaker.refresh_from_db()
    assert speaker.debater == replacement
    assert speaker.hidden_from_profile
