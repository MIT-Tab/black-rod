from datetime import date

from django.test import TestCase

from core.models import Debater, Round, RoundStats, School, Team, Tournament
from core.utils.rounds import get_record, get_tab_card_data


class RoundUtilityTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="Rounds University", included_in_oty=True)
        self.debaters = [
            Debater.objects.create(first_name="Alex", last_name="Gov", school=self.school),
            Debater.objects.create(first_name="Blair", last_name="Opp", school=self.school),
        ]
        self.team = Team.objects.create(name="")
        self.team.debaters.add(*self.debaters)
        self.team.update_name()
        self.team.save()

        self.opponent = Team.objects.create(name="Opponent")
        opp_debaters = [
            Debater.objects.create(first_name="Casey", last_name="One", school=self.school),
            Debater.objects.create(first_name="Devon", last_name="Two", school=self.school),
        ]
        self.opponent.debaters.add(*opp_debaters)

        self.tournament = Tournament.objects.create(
            name="Rounds Invitational",
            host=self.school,
            season="2024",
            date=date(2024, 1, 1),
            num_teams=16,
            num_rounds=4,
        )

    def test_get_record_counts_wins_for_both_sides(self):
        Round.objects.create(
            tournament=self.tournament,
            gov=self.team,
            opp=self.opponent,
            round_number=1,
            victor=Round.GOV,
        )
        Round.objects.create(
            tournament=self.tournament,
            gov=self.opponent,
            opp=self.team,
            round_number=2,
            victor=Round.OPP,
        )
        Round.objects.create(
            tournament=self.tournament,
            gov=self.team,
            opp=self.opponent,
            round_number=3,
            victor=Round.GOV_VIA_FORFEIT,
        )
        Round.objects.create(
            tournament=self.tournament,
            gov=self.team,
            opp=self.opponent,
            round_number=4,
            victor=Round.OPP,
        )

        record = get_record(self.tournament, self.team)
        self.assertEqual(record, "3 - 1")

    def test_get_tab_card_data_returns_round_and_stats(self):
        round_match = Round.objects.create(
            tournament=self.tournament,
            gov=self.team,
            opp=self.opponent,
            round_number=1,
            victor=Round.GOV,
        )
        stats_one = RoundStats.objects.create(
            round=round_match,
            debater=self.debaters[0],
            speaks=27.5,
            ranks=1,
            debater_role="PM",
        )
        stats_two = RoundStats.objects.create(
            round=round_match,
            debater=self.debaters[1],
            speaks=26.5,
            ranks=2,
            debater_role="MG",
        )

        tab_data = get_tab_card_data(self.team, self.tournament)
        self.assertEqual(len(tab_data), 1)
        self.assertEqual(tab_data[0]["round"], round_match)
        self.assertEqual(tab_data[0]["stats"], [stats_one, stats_two])

    def test_tab_card_data_returns_none_when_no_rounds(self):
        self.assertIsNone(get_tab_card_data(self.team, self.tournament))

    def test_get_record_returns_empty_string_without_rounds(self):
        self.assertEqual(get_record(self.tournament, self.team), "")
