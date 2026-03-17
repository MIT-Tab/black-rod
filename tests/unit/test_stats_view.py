from datetime import date

import pytest
from django.core.cache import cache

from core.models import (
    Debater,
    Round,
    School,
    SpeakerResult,
    Team,
    TeamResult,
    Tournament,
)


@pytest.mark.django_db
def test_stats_counts_tournaments_from_all_sources(client):
    cache.clear()

    school = School.objects.create(name="Test School", short_name="TSU")
    partner = Debater.objects.create(
        first_name="Jamie",
        last_name="Lee",
        school=school,
    )
    debater = Debater.objects.create(
        first_name="Alex",
        last_name="Rivera",
        school=school,
    )
    team = Team.objects.create(name="Test Team", short_name="TT")
    team.debaters.set([debater, partner])

    opponent_school = School.objects.create(name="Opponent U", short_name="OPP")
    opponent_debaters = [
        Debater.objects.create(
            first_name="Opp",
            last_name=str(i),
            school=opponent_school,
        )
        for i in range(2)
    ]
    opponent_team = Team.objects.create(name="Opponent Team", short_name="OT")
    opponent_team.debaters.set(opponent_debaters)

    tournament_one = Tournament.objects.create(
        name="Invitational One",
        short_name="Inv One",
        host=school,
        season="2024",
        date=date(2024, 1, 15),
        num_rounds=5,
        num_teams=10,
        num_novice_teams=2,
        num_debaters=20,
        num_novice_debaters=4,
    )
    tournament_two = Tournament.objects.create(
        name="Invitational Two",
        short_name="Inv Two",
        host=school,
        season="2024",
        date=date(2024, 2, 15),
        num_rounds=5,
        num_teams=12,
        num_novice_teams=3,
        num_debaters=24,
        num_novice_debaters=6,
    )
    tournament_three = Tournament.objects.create(
        name="Invitational Three",
        short_name="Inv Three",
        host=school,
        season="2024",
        date=date(2024, 3, 15),
        num_rounds=5,
        num_teams=14,
        num_novice_teams=4,
        num_debaters=28,
        num_novice_debaters=8,
    )

    Round.objects.create(
        round_number=1,
        gov=team,
        opp=opponent_team,
        tournament=tournament_one,
    )

    SpeakerResult.objects.create(
        tournament=tournament_two,
        debater=debater,
        place=1,
    )

    TeamResult.objects.create(
        tournament=tournament_three,
        team=team,
        place=2,
    )

    response = client.get("/stats/")

    assert response.status_code == 200
    leaderboard = response.context["debaters_by_tournament_count"]
    assert leaderboard[0] == debater
    assert leaderboard[0].tournament_count == 3


@pytest.mark.django_db
def test_stats_ignores_synthetic_debaters_in_tournament_attendance(client):
    cache.clear()

    school = School.objects.create(name="Synthetic Test School", short_name="STS")
    real_debater = Debater.objects.create(
        first_name="Real",
        last_name="Debater",
        school=school,
    )
    synthetic_debater = Debater.all_objects.create(
        first_name="Synthetic",
        last_name="Debater",
        school=school,
        synthetic=True,
    )

    real_team = Team.objects.create(name="Real Team", short_name="RT")
    real_team.debaters.set([real_debater])
    synthetic_team = Team.objects.create(name="Synthetic Team", short_name="ST")
    synthetic_team.debaters.set([synthetic_debater])

    tournament = Tournament.objects.create(
        name="Synthetic Invitational",
        short_name="Synth Inv",
        host=school,
        season="2024",
        date=date(2024, 4, 15),
        num_rounds=5,
        num_teams=8,
        num_novice_teams=0,
        num_debaters=16,
        num_novice_debaters=0,
    )

    Round.objects.create(
        round_number=1,
        gov=real_team,
        opp=synthetic_team,
        tournament=tournament,
    )
    SpeakerResult.objects.create(
        tournament=tournament,
        debater=synthetic_debater,
        place=1,
    )
    TeamResult.objects.create(
        tournament=tournament,
        team=synthetic_team,
        place=2,
    )

    response = client.get("/stats/")

    assert response.status_code == 200
    leaderboard_ids = [debater.id for debater in response.context["debaters_by_tournament_count"]]
    assert synthetic_debater.id not in leaderboard_ids
