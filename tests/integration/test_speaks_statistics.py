from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from core.models import Debater, Round, RoundStats, School, SpeakerResult, Team, TeamResult, Tournament
from core.utils.speaks_statistics import get_speaks_statistics, summarize_sample


class SpeaksStatisticsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser(
            username="speaks-admin", email="admin@example.com", password="password",
        )
        cls.school = School.objects.create(name="Speaks University")
        cls.teams = []
        cls.debaters = []
        for index in range(4):
            team = Team.objects.create(name=f"Team {index}")
            members = [Debater.objects.create(first_name=f"Speaker {index}-{slot}")
                       for slot in range(2)]
            team.debaters.add(*members)
            cls.teams.append(team)
            cls.debaters.append(members)

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse("core:speaks_statistics")
        self.tournament = self.make_tournament("2024")

    def make_tournament(self, season):
        tournament = Tournament.objects.create(
            host=self.school, season=season, date=date(int(season), 10, 1), num_rounds=2,
        )
        speaks = ((31, 30), (35, 34), (30, 29), (28, 27))
        for number in (1, 2):
            for gov, opp, victor in ((0, 1, Round.GOV),
                                     (2, 3, Round.GOV if number == 1 else Round.OPP)):
                round_obj = Round.objects.create(
                    tournament=tournament, gov=self.teams[gov], opp=self.teams[opp],
                    round_number=number, victor=victor,
                )
                for team_index in (gov, opp):
                    for slot, member in enumerate(self.debaters[team_index]):
                        RoundStats.objects.create(
                            round=round_obj, debater=member,
                            speaks=speaks[team_index][slot], ranks=slot + 1,
                        )
        for place, team_index in enumerate((0, 2), start=1):
            TeamResult.objects.create(tournament=tournament, team=self.teams[team_index], place=place)
        TeamResult.objects.create(
            tournament=tournament, team=self.teams[3], place=1, type_of_place=TeamResult.NOVICE,
        )
        for team_index, slot, division, place in (
            (1, 0, SpeakerResult.VARSITY, 1),
            (2, 1, SpeakerResult.VARSITY, 10),
            (3, 0, SpeakerResult.NOVICE, 1),
        ):
            SpeakerResult.objects.create(
                tournament=tournament, debater=self.debaters[team_index][slot],
                type_of_place=division, place=place,
            )
        return tournament

    def metrics(self):
        return {row["key"]: row for row in get_speaks_statistics()[0]["metrics"]}

    def test_all_metrics_and_both_seed_groups(self):
        metrics = self.metrics()
        self.assertEqual(len(metrics), 9)
        self.assertEqual(metrics["breaking_teams"]["sample_size"], 2)
        self.assertEqual(
            [metrics["breaking_teams"][f"p{p}"] for p in (25, 50, 75)],
            [Decimal(119), Decimal(120), Decimal(121)],
        )
        expected = {
            "novice_breaking_teams": 110,
            "top_breaking_seed": 122,
            "bottom_breaking_seed": 118,
            "top_full_field_seed": 122,
            "bottom_full_field_seed": 138,
            "first_varsity_speaker": 70,
            "tenth_varsity_speaker": 58,
            "first_novice_speaker": 56,
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(metrics[key]["sample_size"], 1)
                self.assertEqual([metrics[key][f"p{p}"] for p in (25, 50, 75)], [value] * 3)

    def test_page_and_admin_tools_link(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2024-25")
        self.assertContains(response, "Sample size (n)")
        self.assertContains(response, "119.00")
        self.assertContains(response, "Top breaking seed")
        self.assertContains(response, "Bottom full-field seed")
        self.assertContains(self.client.get(reverse("core:admin_tools")), self.url)

    @patch("core.views.speaks_statistics_views.get_speaks_statistics")
    def test_anonymous_regular_staff_and_preaccess_users_cannot_access(self, get_statistics):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        for staff in (False, True):
            user = get_user_model().objects.create_user(username=f"user-{staff}", is_staff=staff)
            user.user_permissions.add(Permission.objects.get(codename="exclusive_pre_access"))
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code, 403)
        get_statistics.assert_not_called()

    def test_seasons_are_dynamic_and_newest_first(self):
        self.make_tournament("2030")
        Tournament.objects.create(host=self.school, season="2031", date=date(2031, 10, 1))
        self.assertEqual([row["season"] for row in get_speaks_statistics()], ["2030", "2024"])

    def test_panels_are_averaged_before_totalling(self):
        original = RoundStats.objects.get(
            round__tournament=self.tournament, round__round_number=1, debater=self.debaters[1][0],
        )
        original.speaks = 34
        original.save()
        RoundStats.objects.create(
            round=original.round, debater=original.debater, score_index=2, speaks=36, ranks=1,
        )
        self.assertEqual(self.metrics()["first_varsity_speaker"]["p50"], 70)

    def test_missing_scores_reduce_samples_and_prevent_inferred_seed_extremes(self):
        RoundStats.objects.filter(
            round__tournament=self.tournament, round__round_number=1, debater=self.debaters[0][0],
        ).update(speaks=None)
        metrics = self.metrics()
        self.assertEqual(metrics["breaking_teams"]["sample_size"], 1)
        self.assertEqual(metrics["breaking_teams"]["p50"], 118)
        self.assertEqual(metrics["top_breaking_seed"]["sample_size"], 0)
        self.assertEqual(metrics["bottom_full_field_seed"]["sample_size"], 0)
        self.assertContains(self.client.get(self.url), "—")

    def test_zero_speaks_are_not_complete_team_scores(self):
        RoundStats.objects.filter(
            round__tournament=self.tournament, debater=self.debaters[0][0],
        ).update(speaks=0)
        self.assertEqual(self.metrics()["breaking_teams"]["sample_size"], 1)

    def test_duplicate_round_numbers_are_not_complete_cards(self):
        original = Round.objects.filter(tournament=self.tournament, gov=self.teams[0]).first()
        duplicate = Round.objects.create(
            tournament=self.tournament, gov=original.gov, opp=original.opp,
            round_number=original.round_number, victor=original.victor,
        )
        for stat in original.stats.all():
            RoundStats.objects.create(round=duplicate, debater=stat.debater, speaks=stat.speaks)
        metrics = self.metrics()
        self.assertEqual(metrics["breaking_teams"]["sample_size"], 1)
        self.assertEqual(metrics["first_varsity_speaker"]["sample_size"], 0)

    def test_eliminations_identify_breakers_without_polluting_prelim_totals(self):
        TeamResult.objects.filter(tournament=self.tournament).delete()
        for stage, number in ((Round.Stage.OUTROUND, 1), (Round.Stage.PRELIM, 3)):
            round_obj = Round.objects.create(
                tournament=self.tournament, gov=self.teams[0], opp=self.teams[2],
                stage=stage, round_number=number,
            )
            # Legacy eliminations may have been imported as prelims with scores.
            for member in self.debaters[0] + self.debaters[2]:
                RoundStats.objects.create(round=round_obj, debater=member, speaks=99)
        metrics = self.metrics()
        self.assertEqual(metrics["breaking_teams"]["sample_size"], 2)
        self.assertEqual(metrics["breaking_teams"]["p50"], 120)
        self.assertEqual(metrics["tenth_varsity_speaker"]["p50"], 58)

    def test_novice_elimination_appearances_are_counted_once(self):
        Round.objects.create(
            tournament=self.tournament, gov=self.teams[2], opp=self.teams[3],
            stage=Round.Stage.OUTROUND, division=Round.Division.NOVICE, round_number=3,
        )
        metrics = self.metrics()
        self.assertEqual(metrics["novice_breaking_teams"]["sample_size"], 2)
        self.assertEqual(metrics["novice_breaking_teams"]["p50"], 114)

    def test_unknown_outcomes_only_exclude_seed_samples(self):
        Round.objects.filter(tournament=self.tournament, gov=self.teams[0]).update(victor=Round.UNKNOWN)
        metrics = self.metrics()
        self.assertEqual(metrics["breaking_teams"]["sample_size"], 2)
        self.assertEqual(metrics["top_breaking_seed"]["sample_size"], 0)
        self.assertEqual(metrics["top_full_field_seed"]["sample_size"], 0)

    def test_speaker_ties_follow_displayed_places(self):
        SpeakerResult.objects.create(
            tournament=self.tournament, debater=self.debaters[0][0],
            type_of_place=SpeakerResult.VARSITY, place=2, tie=True,
        )
        metrics = self.metrics()
        self.assertEqual(metrics["first_varsity_speaker"]["sample_size"], 2)
        self.assertEqual(metrics["first_varsity_speaker"]["p50"], 66)

    def test_seed_rank_tiebreak_and_unresolved_ties(self):
        Round.objects.filter(
            tournament=self.tournament, gov=self.teams[0], round_number=2,
        ).update(victor=Round.OPP)
        RoundStats.objects.filter(round__tournament=self.tournament).update(speaks=30)
        for index, members in enumerate(self.debaters):
            RoundStats.objects.filter(
                round__tournament=self.tournament, debater__in=members,
            ).update(ranks=index + 1)
        metrics = self.metrics()
        self.assertEqual(metrics["top_full_field_seed"]["sample_size"], 1)
        self.assertEqual(metrics["bottom_full_field_seed"]["sample_size"], 1)
        RoundStats.objects.filter(round__tournament=self.tournament).update(ranks=None)
        metrics = self.metrics()
        self.assertEqual(metrics["top_full_field_seed"]["sample_size"], 4)
        self.assertEqual(metrics["bottom_full_field_seed"]["sample_size"], 4)
        self.assertEqual(metrics["top_breaking_seed"]["sample_size"], 2)

    def test_missing_field_participants_prevent_full_field_seed_samples(self):
        self.tournament.num_teams = 8
        self.tournament.save()
        metrics = self.metrics()
        self.assertEqual(metrics["top_full_field_seed"]["sample_size"], 0)
        self.assertEqual(metrics["bottom_full_field_seed"]["sample_size"], 0)
        self.assertEqual(metrics["top_breaking_seed"]["sample_size"], 1)

    def test_empty_page_and_missing_metrics(self):
        RoundStats.objects.all().delete()
        self.assertEqual(get_speaks_statistics(), [])
        self.assertContains(self.client.get(self.url), "No complete speaks data is available yet.")

    def test_empty_and_interpolated_samples(self):
        self.assertEqual(summarize_sample([]), {"sample_size": 0, "p25": None, "p50": None, "p75": None})
        self.assertEqual(summarize_sample([Decimal(10), Decimal(20)])["p25"], Decimal("12.5"))
