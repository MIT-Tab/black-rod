from decimal import Decimal
from datetime import date

import pytest
from django.conf import settings

from core.forms import MergeDebaterRequestForm
from core.models import (
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
