"""Behavioural tests for round utilities."""

from datetime import date

import pytest

from core.models import Debater, Round, RoundStats, School, Team, Tournament
from core.utils import rounds


@pytest.fixture
def tournament(db):  # pylint: disable=redefined-outer-name
    school = School.objects.create(name="Round Utils School", included_in_oty=True)
    tournament = Tournament.objects.create(
        name="Round Utils Invitational",
        host=school,
        date=date(2024, 1, 1),
        season="2024",
        num_rounds=3,
    )
    return tournament


@pytest.fixture
def team_with_debaters(tournament):  # pylint: disable=redefined-outer-name
    team = Team.objects.create(name="Placeholder")
    debater_one = Debater.objects.create(
        first_name="Alice", last_name="Ng", school=tournament.host
    )
    debater_two = Debater.objects.create(
        first_name="Blair", last_name="Ortiz", school=tournament.host
    )
    team.debaters.set([debater_one, debater_two])
    return team


def test_get_record_counts_government_and_opposition_wins(tournament, team_with_debaters):
    team = team_with_debaters
    opponent = Team.objects.create(name="Opposition")

    Round.objects.create(
        tournament=tournament,
        round_number=1,
        gov=team,
        opp=opponent,
        victor=Round.GOV,
    )
    Round.objects.create(
        tournament=tournament,
        round_number=2,
        gov=opponent,
        opp=team,
        victor=Round.OPP,
    )

    record = rounds.get_record(tournament, team)

    assert record == "2 - 1"


def test_get_record_returns_empty_string_without_rounds(tournament, team_with_debaters):
    assert rounds.get_record(tournament, team_with_debaters) == ""


def test_get_tab_card_data_returns_stats_for_each_debater(tournament, team_with_debaters):
    team = team_with_debaters
    opponent = Team.objects.create(name="Opposition")
    round_obj = Round.objects.create(
        tournament=tournament,
        round_number=1,
        gov=team,
        opp=opponent,
        victor=Round.GOV,
    )

    debater_one, debater_two = team.debaters.all()
    RoundStats.objects.create(
        round=round_obj,
        debater=debater_one,
        speaks=26.5,
        ranks=1,
        debater_role="PM",
    )
    RoundStats.objects.create(
        round=round_obj,
        debater=debater_two,
        speaks=25.75,
        ranks=2,
        debater_role="MG",
    )

    tab_data = rounds.get_tab_card_data(team, tournament)

    assert len(tab_data) == 1
    assert tab_data[0]["round"] == round_obj
    stats_debaters = {stat.debater for stat in tab_data[0]["stats"] if stat}
    assert stats_debaters == {debater_one, debater_two}


def test_get_tab_card_data_returns_none_for_missing_team(tournament):
    assert rounds.get_tab_card_data(None, tournament) is None


def test_get_tab_card_data_returns_none_without_rounds(tournament, team_with_debaters):
    assert rounds.get_tab_card_data(team_with_debaters, tournament) is None
