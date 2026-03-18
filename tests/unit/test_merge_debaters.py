from decimal import Decimal
from datetime import date

import pytest
from django.conf import settings

from core.forms import MergeDebaterRequestForm
from core.models import (
    DebaterAlias,
    Debater,
    NOTY,
    OnlineQUAL,
    QUAL,
    QualPoints,
    Reaff,
    Round,
    RoundStats,
    School,
    SchoolAdmin,
    SOTY,
    SpeakerResult,
    Team,
    TeamResult,
    Tournament,
    User,
    Video,
)
from core.utils.merge import MergeError, merge_debaters


@pytest.mark.django_db
def test_merge_request_form_validation():
    user = User.objects.create_user(username="admin", password="test")
    school_one = School.objects.create(name="Alpha University")
    school_two = School.objects.create(name="Beta College")
    SchoolAdmin.objects.create(user=user, school=school_one)

    current_season = settings.CURRENT_SEASON
    prev_season = str(int(settings.CURRENT_SEASON) - 1)

    debater_one = Debater.objects.create(
        first_name="Alex",
        last_name="One",
        school=school_one,
        first_season=prev_season,
        latest_season=current_season,
    )
    debater_two = Debater.objects.create(
        first_name="Blake",
        last_name="Two",
        school=school_two,
        first_season=prev_season,
        latest_season=current_season,
    )

    form = MergeDebaterRequestForm(
        data={
            "school_one": school_one.pk,
            "debater_one": debater_one.pk,
            "school_two": school_two.pk,
            "debater_two": debater_two.pk,
            "keep_debater": "debater_one",
        },
        user=user,
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["primary_debater"] == debater_one
    assert form.cleaned_data["secondary_debater"] == debater_two


@pytest.mark.django_db
def test_merge_debaters_reassigns_related_records():
    school = School.objects.create(name="Merge School")
    partner = Debater.objects.create(
        first_name="Partner",
        last_name="Pat",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    primary = Debater.objects.create(
        first_name="Primary",
        last_name="Person",
        school=school,
        first_season=str(int(settings.CURRENT_SEASON) - 2),
        latest_season=settings.CURRENT_SEASON,
        status=Debater.VARSITY,
    )
    secondary = Debater.objects.create(
        first_name="Secondary",
        last_name="Dup",
        school=school,
        first_season=str(int(settings.CURRENT_SEASON) - 3),
        latest_season=settings.CURRENT_SEASON,
        status=Debater.NOVICE,
    )

    tournament = Tournament.objects.create(
        name="Test Tournament",
        season=settings.CURRENT_SEASON,
        date=date.today(),
        host=school,
        num_teams=10,
        num_debaters=20,
        num_novice_debaters=4,
        num_novice_teams=2,
    )

    team = Team.objects.create(name="Temp Team")
    team.debaters.set([secondary, partner])
    team.update_name()
    team.save()

    opponent_partner = Debater.objects.create(
        first_name="Opp",
        last_name="One",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    opponent = Debater.objects.create(
        first_name="Opp",
        last_name="Two",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    opp_team = Team.objects.create(name="Opp Team")
    opp_team.debaters.set([opponent_partner, opponent])
    opp_team.update_name()
    opp_team.save()

    team_result = TeamResult.objects.create(
        tournament=tournament,
        team=team,
        type_of_place=Debater.VARSITY,
        place=1,
    )
    SpeakerResult.objects.create(
        tournament=tournament,
        debater=secondary,
        type_of_place=Debater.VARSITY,
        place=2,
    )
    round_record = Round.objects.create(
        gov=team,
        opp=opp_team,
        tournament=tournament,
        round_number=1,
    )
    RoundStats.objects.create(
        debater=secondary,
        round=round_record,
        speaks=Decimal("26.5"),
        ranks=Decimal("1.0"),
    )

    Video.objects.create(
        pm=secondary,
        lo=partner,
        mg=opponent_partner,
        mo=opponent,
        tournament=tournament,
        round=Video.VF,
        link="https://example.com/video",
    )

    QualPoints.objects.create(
        debater=secondary,
        season=settings.CURRENT_SEASON,
        points=4.0,
    )
    QUAL.objects.create(
        debater=secondary,
        season=settings.CURRENT_SEASON,
        qual_type=QUAL.POINTS,
    )
    SOTY.objects.create(
        debater=secondary,
        season=settings.CURRENT_SEASON,
        points=12,
        place=5,
    )
    NOTY.objects.create(
        debater=secondary,
        season=settings.CURRENT_SEASON,
        points=8,
        place=4,
    )
    OnlineQUAL.objects.create(
        debater=secondary,
        season=settings.CURRENT_SEASON,
        points=6,
        place=3,
    )
    Reaff.objects.create(
        season=settings.CURRENT_SEASON,
        old_debater=secondary,
        new_debater=primary,
        reaff_date=date.today(),
    )
    secondary.elo_manual_opt = Debater.EloManualOpt.OPT_OUT
    secondary.save(update_fields=["elo_manual_opt"])
    alias = DebaterAlias.objects.create(
        source_name="Secondary Dup",
        normalized_name="secondary dup",
        debater=secondary,
    )

    result_meta = merge_debaters(primary, secondary)

    assert Debater.objects.filter(pk=secondary.pk).exists() is False
    team.refresh_from_db()
    assert primary in team.debaters.all()
    assert secondary not in team.debaters.all()

    primary.refresh_from_db()
    assert primary.first_season == str(int(settings.CURRENT_SEASON) - 3)

    speaker = SpeakerResult.objects.get()
    assert speaker.debater == primary

    round_stats = RoundStats.objects.get(round=round_record)
    assert round_stats.debater == primary

    video = Video.objects.get(tournament=tournament)
    assert video.pm == primary

    assert QualPoints.objects.filter(debater=primary, season=settings.CURRENT_SEASON).exists()
    assert QUAL.objects.filter(debater=secondary).count() == 0
    assert SOTY.objects.filter(debater=secondary).count() == 0
    assert NOTY.objects.filter(debater=secondary).count() == 0
    assert OnlineQUAL.objects.filter(debater=secondary).count() == 0
    assert Reaff.objects.filter(new_debater=primary).exists()
    primary.refresh_from_db()
    assert primary.elo_manual_opt == Debater.EloManualOpt.OPT_OUT

    alias.refresh_from_db()
    assert alias.debater_id == primary.id

    assert result_meta["primary_id"] == primary.pk
    assert str(settings.CURRENT_SEASON) in result_meta["seasons"]


@pytest.mark.django_db
def test_merge_debaters_same_raises_error():
    school = School.objects.create(name="Error School")
    debater = Debater.objects.create(
        first_name="Solo",
        last_name="Debater",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )

    with pytest.raises(MergeError):
        merge_debaters(debater, debater)


@pytest.mark.django_db
def test_merge_debaters_reindexes_conflicting_round_stats():
    school = School.objects.create(name="RoundStats School")
    primary = Debater.objects.create(
        first_name="Theo",
        last_name="Primary",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    secondary = Debater.objects.create(
        first_name="Theo",
        last_name="Secondary",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    opp_one = Debater.objects.create(
        first_name="Opp",
        last_name="One",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    opp_two = Debater.objects.create(
        first_name="Opp",
        last_name="Two",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    gov_team = Team.objects.create(name="Gov Team")
    gov_team.debaters.set([primary, secondary])
    opp_team = Team.objects.create(name="Opp Team")
    opp_team.debaters.set([opp_one, opp_two])
    tournament = Tournament.objects.create(
        name="RoundStats Invitational",
        season=settings.CURRENT_SEASON,
        date=date.today(),
        host=school,
        num_teams=8,
        num_debaters=16,
        num_novice_debaters=0,
        num_novice_teams=0,
    )
    round_record = Round.objects.create(
        gov=gov_team,
        opp=opp_team,
        tournament=tournament,
        round_number=1,
    )
    primary_stat = RoundStats.objects.create(
        round=round_record,
        debater=primary,
        speaks=Decimal("30.0000"),
        ranks=Decimal("1.0000"),
        debater_role="PM",
        score_index=1,
    )
    secondary_stat = RoundStats.objects.create(
        round=round_record,
        debater=secondary,
        speaks=Decimal("29.0000"),
        ranks=Decimal("2.0000"),
        debater_role="MG",
        score_index=1,
    )

    merge_debaters(primary, secondary)

    stats = list(
        RoundStats.objects.filter(round=round_record, debater=primary).order_by("score_index", "id")
    )
    assert [stat.score_index for stat in stats] == [1, 2]
    assert stats[0].id == primary_stat.id
    assert stats[1].id == secondary_stat.id
    assert stats[1].debater_role == "MG"
    assert stats[1].speaks == Decimal("29.0000")


@pytest.mark.django_db
def test_merge_debaters_updates_existing_qual_with_missing_details():
    school = School.objects.create(name="Qual School")
    primary = Debater.objects.create(
        first_name="Primary",
        last_name="Qual",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    secondary = Debater.objects.create(
        first_name="Secondary",
        last_name="Qual",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    tournament = Tournament.objects.create(
        name="Qual Event",
        season=settings.CURRENT_SEASON,
        date=date.today(),
        host=school,
        num_teams=10,
        num_debaters=20,
        num_novice_debaters=4,
        num_novice_teams=2,
    )

    QUAL.objects.create(
        debater=primary,
        season=settings.CURRENT_SEASON,
        qual_type=QUAL.POINTS,
        tournament=None,
        place=-1,
        points=-1,
    )
    QUAL.objects.create(
        debater=secondary,
        season=settings.CURRENT_SEASON,
        qual_type=QUAL.POINTS,
        tournament=tournament,
        place=2,
        points=10,
        tied=True,
    )

    SpeakerResult.objects.create(
        tournament=tournament,
        debater=primary,
        type_of_place=Debater.VARSITY,
        place=1,
    )

    merge_debaters(primary, secondary)

    quals = QUAL.objects.filter(
        debater=primary,
        season=settings.CURRENT_SEASON,
        qual_type=QUAL.POINTS,
    )
    assert quals.count() == 1

    qual = quals.get()
    assert qual.tournament == tournament
    assert qual.place == 2
    assert qual.points == 10
    assert qual.tied is True


@pytest.mark.django_db
def test_merge_debaters_retains_existing_qual_details_when_present():
    school = School.objects.create(name="Qual School Two")
    primary = Debater.objects.create(
        first_name="Primary",
        last_name="Existing",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    secondary = Debater.objects.create(
        first_name="Secondary",
        last_name="Existing",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    tournament_primary = Tournament.objects.create(
        name="Primary Event",
        season=settings.CURRENT_SEASON,
        date=date.today(),
        host=school,
        num_teams=10,
        num_debaters=20,
        num_novice_debaters=4,
        num_novice_teams=2,
    )
    tournament_secondary = Tournament.objects.create(
        name="Secondary Event",
        season=settings.CURRENT_SEASON,
        date=date.today(),
        host=school,
        num_teams=10,
        num_debaters=20,
        num_novice_debaters=4,
        num_novice_teams=2,
    )

    QUAL.objects.create(
        debater=primary,
        season=settings.CURRENT_SEASON,
        qual_type=QUAL.POINTS,
        tournament=tournament_primary,
        place=3,
        points=9,
    )
    QUAL.objects.create(
        debater=secondary,
        season=settings.CURRENT_SEASON,
        qual_type=QUAL.POINTS,
        tournament=tournament_secondary,
        place=5,
        points=7,
    )

    SpeakerResult.objects.create(
        tournament=tournament_primary,
        debater=primary,
        type_of_place=Debater.VARSITY,
        place=1,
    )

    merge_debaters(primary, secondary)

    quals = QUAL.objects.filter(
        debater=primary,
        season=settings.CURRENT_SEASON,
        qual_type=QUAL.POINTS,
    )
    assert quals.count() == 1

    qual = quals.get()
    assert qual.tournament == tournament_primary
    assert qual.place == 3
    assert qual.points == 9


@pytest.mark.django_db
def test_merge_debaters_handles_existing_autoqual_previous_season():
    school = School.objects.create(name="Legacy Qual School")
    prev_season = str(int(settings.CURRENT_SEASON) - 1)

    partner = Debater.objects.create(
        first_name="Partner",
        last_name="Prev",
        school=school,
        first_season=prev_season,
        latest_season=prev_season,
    )
    primary = Debater.objects.create(
        first_name="Primary",
        last_name="Prev",
        school=school,
        first_season=prev_season,
        latest_season=prev_season,
    )
    secondary = Debater.objects.create(
        first_name="Secondary",
        last_name="Prev",
        school=school,
        first_season=prev_season,
        latest_season=prev_season,
    )

    tournament = Tournament.objects.create(
        name="Legacy Event",
        season=prev_season,
        date=date.today(),
        host=school,
        num_teams=10,
        num_debaters=20,
        num_novice_debaters=4,
        num_novice_teams=2,
        autoqual_bar=4,
        qual_type=QUAL.EXPANSION,
    )

    team = Team.objects.create(name="Legacy Team")
    team.debaters.set([secondary, partner])
    team.update_name()
    team.save()

    TeamResult.objects.create(
        tournament=tournament,
        team=team,
        type_of_place=Debater.VARSITY,
        place=2,
    )

    QUAL.objects.create(
        debater=primary,
        season=prev_season,
        qual_type=QUAL.EXPANSION,
    )
    QUAL.objects.create(
        debater=secondary,
        season=prev_season,
        qual_type=QUAL.EXPANSION,
        tournament=tournament,
    )

    merge_debaters(primary, secondary)

    quals = QUAL.objects.filter(
        debater=primary,
        season=prev_season,
        qual_type=QUAL.EXPANSION,
    )
    assert quals.count() == 1
    qual = quals.get()
    assert qual.tournament == tournament


@pytest.mark.django_db
def test_merge_debaters_preserves_passthrough_qual_points_without_rebuild():
    school = School.objects.create(name="Passthrough Qual School")
    season = str(int(settings.CURRENT_SEASON) - 1)

    primary = Debater.objects.create(
        first_name="Primary",
        last_name="Passthrough",
        school=school,
        first_season=season,
        latest_season=season,
    )
    secondary = Debater.objects.create(
        first_name="Secondary",
        last_name="Passthrough",
        school=school,
        first_season=season,
        latest_season=season,
    )

    QualPoints.objects.create(debater=primary, season=season, points=4.0)
    QualPoints.objects.create(debater=secondary, season=season, points=9.0)

    merge_debaters(primary, secondary)

    merged_rows = QualPoints.objects.filter(debater=primary, season=season)
    assert merged_rows.count() == 1
    assert merged_rows.get().points == 13.0
    assert QUAL.objects.filter(
        debater=primary,
        season=season,
        qual_type=QUAL.POINTS,
    ).exists()


@pytest.mark.django_db
def test_merge_debaters_recomputes_qual_points_for_merged_debater_only():
    school = School.objects.create(name="Current Merge School")
    season = str(settings.CURRENT_SEASON)

    partner = Debater.objects.create(
        first_name="Partner",
        last_name="Curr",
        school=school,
        first_season=season,
        latest_season=season,
    )
    primary = Debater.objects.create(
        first_name="Primary",
        last_name="Curr",
        school=school,
        first_season=season,
        latest_season=season,
    )
    secondary = Debater.objects.create(
        first_name="Secondary",
        last_name="Curr",
        school=school,
        first_season=season,
        latest_season=season,
    )

    tournament = Tournament.objects.create(
        name="Current Merge Event",
        season=season,
        date=date.today(),
        host=school,
        num_teams=10,
        num_debaters=20,
        num_novice_debaters=4,
        num_novice_teams=2,
    )

    team = Team.objects.create(name="Current Team")
    team.debaters.set([secondary, partner])
    team.update_name()
    team.save()

    TeamResult.objects.create(
        tournament=tournament,
        team=team,
        type_of_place=Debater.VARSITY,
        place=1,
    )

    other_school = School.objects.create(name="Other Merge School")
    other_debater = Debater.objects.create(
        first_name="Other",
        last_name="Debater",
        school=other_school,
        first_season=season,
        latest_season=season,
    )
    other_partner = Debater.objects.create(
        first_name="Other",
        last_name="Partner",
        school=other_school,
        first_season=season,
        latest_season=season,
    )
    other_tournament = Tournament.objects.create(
        name="Other Event",
        season=season,
        date=date.today(),
        host=other_school,
        num_teams=10,
        num_debaters=20,
        num_novice_debaters=4,
        num_novice_teams=2,
    )
    other_team = Team.objects.create(name="Other Team")
    other_team.debaters.set([other_debater, other_partner])
    other_team.update_name()
    other_team.save()
    TeamResult.objects.create(
        tournament=other_tournament,
        team=other_team,
        type_of_place=Debater.VARSITY,
        place=1,
    )

    merge_debaters(primary, secondary)

    merged_qual_points = QualPoints.objects.filter(debater=primary, season=season)
    assert merged_qual_points.count() == 1
    assert merged_qual_points.get().points == 8
    assert not QUAL.objects.filter(
        debater=primary, season=season, qual_type=QUAL.POINTS
    ).exists()

    # Unrelated debaters should not be recomputed by this merge.
    assert not QualPoints.objects.filter(debater=other_debater, season=season).exists()


@pytest.mark.django_db
def test_merge_debaters_creates_points_qual_when_merged_results_cross_bar():
    school = School.objects.create(name="Threshold Merge School")
    season = str(settings.CURRENT_SEASON)

    primary = Debater.objects.create(
        first_name="Primary",
        last_name="Threshold",
        school=school,
        first_season=season,
        latest_season=season,
    )
    secondary = Debater.objects.create(
        first_name="Secondary",
        last_name="Threshold",
        school=school,
        first_season=season,
        latest_season=season,
    )
    partner_one = Debater.objects.create(
        first_name="Partner",
        last_name="One",
        school=school,
        first_season=season,
        latest_season=season,
    )
    partner_two = Debater.objects.create(
        first_name="Partner",
        last_name="Two",
        school=school,
        first_season=season,
        latest_season=season,
    )

    tournament_one = Tournament.objects.create(
        name="Threshold Event One",
        season=season,
        date=date.today(),
        host=school,
        num_teams=10,
        num_debaters=20,
        num_novice_debaters=4,
        num_novice_teams=2,
    )
    tournament_two = Tournament.objects.create(
        name="Threshold Event Two",
        season=season,
        date=date.today(),
        host=school,
        num_teams=10,
        num_debaters=20,
        num_novice_debaters=4,
        num_novice_teams=2,
    )

    team_one = Team.objects.create(name="Threshold Team One")
    team_one.debaters.set([primary, partner_one])
    team_one.update_name()
    team_one.save()
    team_two = Team.objects.create(name="Threshold Team Two")
    team_two.debaters.set([secondary, partner_two])
    team_two.update_name()
    team_two.save()

    TeamResult.objects.create(
        tournament=tournament_one,
        team=team_one,
        type_of_place=Debater.VARSITY,
        place=1,
    )
    TeamResult.objects.create(
        tournament=tournament_two,
        team=team_two,
        type_of_place=Debater.VARSITY,
        place=1,
    )

    merge_debaters(primary, secondary)

    qual_points = QualPoints.objects.get(debater=primary, season=season)
    assert qual_points.points == 16
    assert QUAL.objects.filter(
        debater=primary,
        season=season,
        qual_type=QUAL.POINTS,
    ).exists()


@pytest.mark.django_db
def test_merge_debaters_recomputes_soty_points_for_primary():
    school = School.objects.create(name="SOTY Merge School")
    season = str(settings.CURRENT_SEASON)

    primary = Debater.objects.create(
        first_name="Primary",
        last_name="Soty",
        school=school,
        first_season=season,
        latest_season=season,
    )
    secondary = Debater.objects.create(
        first_name="Secondary",
        last_name="Soty",
        school=school,
        first_season=season,
        latest_season=season,
    )

    tournament_one = Tournament.objects.create(
        name="SOTY Event One",
        season=season,
        date=date.today(),
        host=school,
        num_teams=16,
        num_debaters=32,
        num_novice_debaters=4,
        num_novice_teams=2,
    )
    tournament_two = Tournament.objects.create(
        name="SOTY Event Two",
        season=season,
        date=date.today(),
        host=school,
        num_teams=16,
        num_debaters=32,
        num_novice_debaters=4,
        num_novice_teams=2,
    )

    SpeakerResult.objects.create(
        tournament=tournament_one,
        debater=primary,
        type_of_place=Debater.VARSITY,
        place=2,
    )
    SpeakerResult.objects.create(
        tournament=tournament_two,
        debater=secondary,
        type_of_place=Debater.VARSITY,
        place=1,
    )

    SOTY.objects.create(debater=primary, season=season, points=1, place=99)
    SOTY.objects.create(debater=secondary, season=season, points=2, place=98)

    merge_debaters(primary, secondary)

    soty = SOTY.objects.get(debater=primary, season=season)
    assert soty.points == 21.5


@pytest.mark.django_db
def test_merge_debaters_reflows_soty_places_for_other_rows():
    school = School.objects.create(name="SOTY Reflow School")
    season = str(settings.CURRENT_SEASON)

    primary = Debater.objects.create(
        first_name="Primary",
        last_name="Reflow",
        school=school,
        first_season=season,
        latest_season=season,
    )
    secondary = Debater.objects.create(
        first_name="Secondary",
        last_name="Reflow",
        school=school,
        first_season=season,
        latest_season=season,
    )
    other = Debater.objects.create(
        first_name="Other",
        last_name="Reflow",
        school=school,
        first_season=season,
        latest_season=season,
    )

    tournament_one = Tournament.objects.create(
        name="Reflow Event One",
        season=season,
        date=date.today(),
        host=school,
        num_teams=16,
        num_debaters=32,
        num_novice_debaters=4,
        num_novice_teams=2,
    )
    tournament_two = Tournament.objects.create(
        name="Reflow Event Two",
        season=season,
        date=date.today(),
        host=school,
        num_teams=16,
        num_debaters=32,
        num_novice_debaters=4,
        num_novice_teams=2,
    )

    SpeakerResult.objects.create(
        tournament=tournament_one,
        debater=primary,
        type_of_place=Debater.VARSITY,
        place=2,
    )
    SpeakerResult.objects.create(
        tournament=tournament_two,
        debater=secondary,
        type_of_place=Debater.VARSITY,
        place=1,
    )

    SOTY.objects.create(debater=primary, season=season, points=1, place=3)
    SOTY.objects.create(debater=secondary, season=season, points=2, place=2)
    SOTY.objects.create(debater=other, season=season, points=20, place=1)

    merge_debaters(primary, secondary)

    merged_soty = SOTY.objects.get(debater=primary, season=season)
    other_soty = SOTY.objects.get(debater=other, season=season)
    assert merged_soty.place == 1
    assert other_soty.place == 2
    assert other_soty.points == 20


@pytest.mark.django_db
def test_merge_debaters_uses_historical_qual_bar_for_passthrough_points(settings):
    school = School.objects.create(name="Historical Merge School")
    season = str(int(settings.CURRENT_SEASON) - 1)
    settings.HISTORICAL_QUAL_BARS = {season: 9.0}
    settings.QUAL_BAR = 11.5

    primary = Debater.objects.create(
        first_name="Primary",
        last_name="Historical",
        school=school,
        first_season=season,
        latest_season=season,
    )
    secondary = Debater.objects.create(
        first_name="Secondary",
        last_name="Historical",
        school=school,
        first_season=season,
        latest_season=season,
    )

    QualPoints.objects.create(debater=primary, season=season, points=4.0)
    QualPoints.objects.create(debater=secondary, season=season, points=6.0)

    merge_debaters(primary, secondary)

    merged_row = QualPoints.objects.get(debater=primary, season=season)
    assert merged_row.points == 10.0
    assert QUAL.objects.filter(
        debater=primary,
        season=season,
        qual_type=QUAL.POINTS,
    ).exists()


@pytest.mark.django_db
def test_merge_debaters_removes_stale_points_qual_when_recomputed_points_drop():
    school = School.objects.create(name="Stale Points School")
    season = str(settings.CURRENT_SEASON)

    primary = Debater.objects.create(
        first_name="Primary",
        last_name="Stale",
        school=school,
        first_season=season,
        latest_season=season,
    )
    secondary = Debater.objects.create(
        first_name="Secondary",
        last_name="Stale",
        school=school,
        first_season=season,
        latest_season=season,
    )
    partner = Debater.objects.create(
        first_name="Partner",
        last_name="Stale",
        school=school,
        first_season=season,
        latest_season=season,
    )

    tournament = Tournament.objects.create(
        name="No Qual Points Event",
        season=season,
        date=date.today(),
        host=school,
        num_teams=10,
        num_debaters=20,
        num_novice_debaters=4,
        num_novice_teams=2,
        qual_type=Tournament.BRANDEIS,
    )

    team = Team.objects.create(name="No Qual Team")
    team.debaters.set([secondary, partner])
    team.update_name()
    team.save()

    TeamResult.objects.create(
        tournament=tournament,
        team=team,
        type_of_place=Debater.VARSITY,
        place=1,
    )

    QualPoints.objects.create(debater=primary, season=season, points=30)
    QUAL.objects.create(debater=primary, season=season, qual_type=QUAL.POINTS)

    merge_debaters(primary, secondary)

    assert not QualPoints.objects.filter(debater=primary, season=season).exists()
    assert not QUAL.objects.filter(
        debater=primary,
        season=season,
        qual_type=QUAL.POINTS,
    ).exists()
