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
