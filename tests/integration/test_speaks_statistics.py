from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from core.models import (
    Debater, DebaterAlias, DebaterAliasGroup, ImportedRoundMetadata, Round, RoundStats,
    School, SpeakerResult, Team, TeamResult, Tournament,
)
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
        self.assertContains(response, "Individual speaks by final team record")
        self.assertContains(response, "Speaks-to-percentile correspondence")
        self.assertContains(response, "N (speak samples)")
        self.assertContains(response, "16 individual speak samples")
        self.assertContains(response, 'data-values-id="speaks-cdf-0"')
        self.assertContains(response, 'id="speaks-cdf-0" type="application/json"')
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

    def test_missing_scores_exclude_only_the_affected_candidates(self):
        RoundStats.objects.filter(
            round__tournament=self.tournament, round__round_number=1, debater=self.debaters[0][0],
        ).update(speaks=None)
        metrics = self.metrics()
        self.assertEqual(metrics["breaking_teams"]["sample_size"], 1)
        self.assertEqual(metrics["breaking_teams"]["p50"], 118)
        self.assertEqual(metrics["top_breaking_seed"]["sample_size"], 1)
        self.assertEqual(metrics["top_breaking_seed"]["p50"], 118)
        self.assertEqual(metrics["bottom_full_field_seed"]["sample_size"], 1)
        self.assertEqual(metrics["bottom_full_field_seed"]["p50"], 138)

    def test_zero_speaks_are_not_complete_team_scores(self):
        RoundStats.objects.filter(
            round__tournament=self.tournament, debater=self.debaters[0][0],
        ).update(speaks=0)
        self.assertEqual(self.metrics()["breaking_teams"]["sample_size"], 1)
        self.assertEqual(get_speaks_statistics()[0]["round_speaks"]["sample_size"], 14)

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
        self.assertEqual(get_speaks_statistics()[0]["round_speaks"]["sample_size"], 16)

    def test_novice_elimination_appearances_are_counted_once(self):
        Round.objects.create(
            tournament=self.tournament, gov=self.teams[2], opp=self.teams[3],
            stage=Round.Stage.OUTROUND, division=Round.Division.NOVICE, round_number=3,
        )
        metrics = self.metrics()
        self.assertEqual(metrics["novice_breaking_teams"]["sample_size"], 2)
        self.assertEqual(metrics["novice_breaking_teams"]["p50"], 114)

    def test_unknown_outcomes_exclude_only_the_affected_seed_candidates(self):
        Round.objects.filter(tournament=self.tournament, gov=self.teams[0]).update(victor=Round.UNKNOWN)
        metrics = self.metrics()
        self.assertEqual(metrics["breaking_teams"]["sample_size"], 2)
        self.assertEqual(metrics["top_breaking_seed"]["sample_size"], 1)
        self.assertEqual(metrics["top_breaking_seed"]["p50"], 118)
        self.assertEqual(metrics["top_full_field_seed"]["sample_size"], 1)
        self.assertEqual(metrics["top_full_field_seed"]["p50"], 118)

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

    def test_reported_field_size_does_not_discard_available_seed_samples(self):
        self.tournament.num_teams = 8
        self.tournament.save()
        metrics = self.metrics()
        self.assertEqual(metrics["top_full_field_seed"]["sample_size"], 1)
        self.assertEqual(metrics["top_full_field_seed"]["p50"], 122)
        self.assertEqual(metrics["bottom_full_field_seed"]["sample_size"], 1)
        self.assertEqual(metrics["top_breaking_seed"]["sample_size"], 1)

    def test_multiple_partially_scored_tournaments_each_contribute_seed_samples(self):
        self.make_tournament("2024")
        self.make_tournament("2024")
        RoundStats.objects.filter(
            debater=self.debaters[1][1], round__round_number=1,
        ).update(speaks=None)
        Tournament.objects.all().update(num_teams=20)
        season = get_speaks_statistics()[0]
        metrics = {row["key"]: row for row in season["metrics"]}
        self.assertEqual(season["scored_tournaments"], 3)
        for key in ("top_breaking_seed", "bottom_breaking_seed",
                    "top_full_field_seed", "bottom_full_field_seed"):
            self.assertEqual(metrics[key]["sample_size"], 3)
            self.assertEqual(metrics[key]["tournament_count"], 3)

    def test_results_match_rounds_by_partnership_instead_of_team_record_id(self):
        for index in (0, 3):
            duplicate = Team.objects.create(name=f"Result team {index}")
            duplicate.debaters.add(*self.debaters[index])
            TeamResult.objects.filter(tournament=self.tournament, team=self.teams[index]).update(team=duplicate)
        metrics = self.metrics()
        self.assertEqual(metrics["breaking_teams"]["sample_size"], 2)
        self.assertEqual(metrics["novice_breaking_teams"]["p50"], 110)
        self.assertEqual(metrics["top_breaking_seed"]["p50"], 122)

    def test_duplicate_result_partnerships_are_not_counted_twice(self):
        duplicate = Team.objects.create(name="Duplicate result")
        duplicate.debaters.add(*self.debaters[0])
        TeamResult.objects.create(tournament=self.tournament, team=duplicate, place=3)
        self.assertEqual(self.metrics()["breaking_teams"]["sample_size"], 2)

    def test_rounds_for_duplicate_team_records_form_one_complete_card(self):
        duplicate = Team.objects.create(name="Second round team record")
        duplicate.debaters.add(*self.debaters[0])
        Round.objects.filter(tournament=self.tournament, gov=self.teams[0], round_number=2).update(gov=duplicate)
        self.assertEqual(self.metrics()["top_breaking_seed"]["p50"], 122)

    def test_linked_speaker_profiles_share_one_tournament_total(self):
        original = self.debaters[1][0]
        alias_group = DebaterAliasGroup.objects.create()
        Debater.all_objects.filter(pk=original.pk).update(alias_group=alias_group)
        linked = Debater.objects.create(first_name="Linked speaker", alias_group=alias_group)
        RoundStats.objects.filter(
            round__tournament=self.tournament, round__round_number=2, debater=original,
        ).update(debater=linked)
        metrics = self.metrics()
        self.assertEqual(metrics["first_varsity_speaker"]["p50"], 70)
        self.assertEqual(metrics["bottom_full_field_seed"]["p50"], 138)

    def test_imported_side_aliases_resolve_placeholder_round_teams(self):
        aliases = [DebaterAlias.objects.create(
            debater=member, source_name=f"Alias {slot}", normalized_name=f"alias {slot}",
        ) for slot, member in enumerate(self.debaters[0])]
        placeholder = Team.objects.create(name="Imported placeholder")
        for round_obj in Round.objects.filter(tournament=self.tournament, gov=self.teams[0]):
            ImportedRoundMetadata.objects.create(
                round=round_obj, gov_1_alias=aliases[0], gov_2_alias=aliases[1],
            )
            Round.objects.filter(pk=round_obj.pk).update(gov=placeholder)
        metrics = self.metrics()
        self.assertEqual(metrics["breaking_teams"]["sample_size"], 2)
        self.assertEqual(metrics["top_breaking_seed"]["p50"], 122)
        self.assertEqual(metrics["top_full_field_seed"]["p50"], 122)

    def test_canonical_metadata_provides_partnerships_and_winners(self):
        placeholder = Team.objects.create(name="Canonical import placeholder")
        metadata = {
            "team_a_ids": [member.pk for member in self.debaters[0]],
            "team_b_ids": [member.pk for member in self.debaters[1]],
            "winner": "a", "stage": "prelim",
        }
        Round.objects.filter(tournament=self.tournament, gov=self.teams[0]).update(
            gov=placeholder, opp=placeholder, victor=Round.UNKNOWN, metadata=metadata,
        )
        metrics = self.metrics()
        self.assertEqual(metrics["top_breaking_seed"]["p50"], 122)
        self.assertEqual(metrics["bottom_full_field_seed"]["p50"], 138)
        columns = {column["label"]: column for column in get_speaks_statistics()[0]["round_speaks"]["columns"]}
        self.assertEqual(columns["2-0"]["sample_size"], 4)

    def test_canonical_winner_takes_precedence_over_the_legacy_victor_field(self):
        Round.objects.filter(tournament=self.tournament, gov=self.teams[0]).update(metadata={"winner": "b"})
        metrics = self.metrics()
        self.assertEqual(metrics["top_breaking_seed"]["p50"], 118)
        self.assertEqual(metrics["top_full_field_seed"]["p50"], 138)

    def test_canonical_stage_excludes_elimination_speaks(self):
        round_obj = Round.objects.create(
            tournament=self.tournament, gov=self.teams[0], opp=self.teams[2],
            round_number=1, metadata={"stage": "outround"},
        )
        for member in self.debaters[0] + self.debaters[2]:
            RoundStats.objects.create(round=round_obj, debater=member, speaks=99)
        metrics = self.metrics()
        self.assertEqual(metrics["breaking_teams"]["p50"], 120)
        self.assertEqual(metrics["top_breaking_seed"]["p50"], 122)

    def test_novice_partnerships_breaking_in_varsity_are_included(self):
        Debater.objects.filter(pk__in=[member.pk for member in self.debaters[0]]).update(first_season="2024")
        metrics = self.metrics()
        self.assertEqual(metrics["novice_breaking_teams"]["sample_size"], 2)
        self.assertEqual(metrics["novice_breaking_teams"]["p50"], 116)

    def test_linked_profiles_do_not_restart_novice_eligibility(self):
        member = self.debaters[0][0]
        group = DebaterAliasGroup.objects.create()
        Debater.objects.filter(pk=member.pk).update(alias_group=group, first_season="2024")
        Debater.objects.create(first_name="Older linked profile", alias_group=group, first_season="2023")
        Debater.objects.filter(pk=self.debaters[0][1].pk).update(first_season="2024")
        self.assertEqual(self.metrics()["novice_breaking_teams"]["sample_size"], 1)

    def test_missing_awards_are_reconstructed_from_summed_speaker_scores(self):
        SpeakerResult.objects.filter(tournament=self.tournament).delete()
        Debater.objects.filter(pk__in=[member.pk for member in self.debaters[3]]).update(first_season="2024")
        metrics = self.metrics()
        self.assertEqual(metrics["first_varsity_speaker"]["p50"], 70)
        self.assertEqual(metrics["first_novice_speaker"]["p50"], 56)
        self.assertEqual(metrics["tenth_varsity_speaker"]["sample_size"], 0)

    def test_tenth_speaker_can_be_reconstructed_without_awards(self):
        SpeakerResult.objects.filter(tournament=self.tournament, place=10).delete()
        for index, speaks in enumerate((25, 26)):
            debater = Debater.objects.create(first_name=f"Unplaced speaker {index}")
            for round_obj in Round.objects.filter(tournament=self.tournament, gov=self.teams[0]):
                RoundStats.objects.create(round=round_obj, debater=debater, speaks=speaks)
        self.assertEqual(self.metrics()["tenth_varsity_speaker"]["p50"], 50)

    def test_missing_official_winner_scores_do_not_substitute_another_speaker(self):
        RoundStats.objects.filter(debater=self.debaters[1][0]).update(speaks=None)
        self.assertEqual(self.metrics()["first_varsity_speaker"]["sample_size"], 0)
        self.assertContains(self.client.get(self.url), "—")

    def test_empty_page_and_missing_metrics(self):
        RoundStats.objects.all().delete()
        self.assertEqual(get_speaks_statistics(), [])
        self.assertContains(self.client.get(self.url), "No usable preliminary speaks data is available yet.")

    def test_individual_speaks_group_by_final_record_with_counts_and_means(self):
        distribution = get_speaks_statistics()[0]["round_speaks"]
        columns = {column["label"]: column for column in distribution["columns"]}
        self.assertEqual(distribution["sample_size"], 16)
        self.assertEqual(distribution["tournament_count"], 1)
        self.assertEqual(columns["2-0"]["sample_size"], 4)
        self.assertEqual(columns["2-0"]["mean"], Decimal("30.5"))
        self.assertEqual(columns["0-2"]["mean"], Decimal("34.5"))
        self.assertEqual(columns["1-1"]["sample_size"], 8)
        self.assertEqual(columns["1-1"]["p25"], Decimal("27.75"))
        self.assertEqual(columns["1-1"]["p50"], Decimal("28.5"))
        self.assertEqual(columns["1-1"]["p75"], Decimal("29.25"))
        self.assertEqual(columns["All records"]["mean"], Decimal("30.5"))

    def test_correspondence_includes_ties_and_unobserved_whole_scores(self):
        distribution = get_speaks_statistics()[0]["round_speaks"]
        rows = {row["score"]: row for row in distribution["correspondence"]}
        self.assertEqual(rows[Decimal(30)]["sample_size"], 4)
        self.assertEqual(rows[Decimal(30)]["cumulative_count"], 10)
        self.assertEqual(rows[Decimal(30)]["percentile"], Decimal("62.5"))
        self.assertEqual(rows[Decimal(33)]["sample_size"], 0)
        self.assertEqual(rows[Decimal(33)]["percentile"], 75)
        self.assertEqual(rows[Decimal(35)]["percentile"], 100)

    def test_individual_samples_from_incomplete_cards_are_retained(self):
        RoundStats.objects.filter(
            round__tournament=self.tournament, round__round_number=1, debater=self.debaters[0][0],
        ).update(speaks=None)
        distribution = get_speaks_statistics()[0]["round_speaks"]
        columns = {column["label"]: column for column in distribution["columns"]}
        self.assertEqual(distribution["sample_size"], 15)
        self.assertEqual(columns["2-0"]["sample_size"], 3)
        self.assertNotIn("Unknown record", columns)

    def test_unknown_records_remain_in_the_overall_distribution(self):
        Round.objects.filter(tournament=self.tournament, gov=self.teams[0], round_number=1).update(victor=Round.UNKNOWN)
        distribution = get_speaks_statistics()[0]["round_speaks"]
        columns = {column["label"]: column for column in distribution["columns"]}
        self.assertEqual(distribution["sample_size"], 16)
        self.assertEqual(columns["Unknown record"]["sample_size"], 8)
        self.assertEqual(columns["1-1"]["sample_size"], 8)
        self.assertEqual(sum(column["sample_size"] for column in distribution["columns"][:-1]), 16)

    def test_panel_ballots_count_as_one_individual_speak_sample(self):
        original = RoundStats.objects.get(
            round__tournament=self.tournament, round__round_number=1, debater=self.debaters[1][0],
        )
        original.speaks = Decimal("34.5")
        original.save()
        RoundStats.objects.create(round=original.round, debater=original.debater, score_index=2, speaks=36)
        distribution = get_speaks_statistics()[0]["round_speaks"]
        rows = {row["score"]: row for row in distribution["correspondence"]}
        self.assertEqual(distribution["sample_size"], 16)
        self.assertEqual(rows[Decimal("35.25")]["sample_size"], 1)
        self.assertEqual(rows[Decimal("35.25")]["percentile"], 100)

    def test_seasons_with_only_partial_cards_still_show_individual_distribution(self):
        Round.objects.filter(tournament=self.tournament, round_number=2).delete()
        season = get_speaks_statistics()[0]
        self.assertTrue(all(metric["sample_size"] == 0 for metric in season["metrics"]))
        self.assertEqual(season["round_speaks"]["sample_size"], 8)
        self.assertContains(self.client.get(self.url), "8 individual speak samples")

    def test_different_tournament_lengths_have_separate_record_columns(self):
        shorter = self.make_tournament("2024")
        Tournament.objects.filter(pk=shorter.pk).update(num_rounds=1)
        distribution = get_speaks_statistics()[0]["round_speaks"]
        self.assertEqual(distribution["sample_size"], 24)
        self.assertEqual([column["label"] for column in distribution["columns"]],
                         ["0-1", "1-0", "0-2", "1-1", "2-0", "All records"])

    def test_five_round_table_includes_all_six_records(self):
        Tournament.objects.filter(pk=self.tournament.pk).update(num_rounds=5)
        distribution = get_speaks_statistics()[0]["round_speaks"]
        self.assertEqual([column["label"] for column in distribution["columns"]],
                         ["0-5", "1-4", "2-3", "3-2", "4-1", "5-0", "Unknown record", "All records"])
        self.assertEqual(distribution["columns"][0]["sample_size"], 0)

    def test_duplicate_speaker_round_numbers_do_not_inflate_the_distribution(self):
        original = Round.objects.filter(tournament=self.tournament, gov=self.teams[0]).first()
        duplicate = Round.objects.create(
            tournament=self.tournament, gov=original.gov, opp=original.opp,
            round_number=original.round_number, victor=original.victor,
        )
        for stat in original.stats.all():
            RoundStats.objects.create(round=duplicate, debater=stat.debater, speaks=stat.speaks)
        self.assertEqual(get_speaks_statistics()[0]["round_speaks"]["sample_size"], 12)

    def test_round_distributions_are_separate_for_each_season(self):
        newer = self.make_tournament("2030")
        RoundStats.objects.filter(round__tournament=newer).update(speaks=10)
        seasons = get_speaks_statistics()
        self.assertEqual([season["round_speaks"]["sample_size"] for season in seasons], [16, 16])
        self.assertEqual(seasons[0]["round_speaks"]["overall"]["mean"], 10)
        self.assertEqual(seasons[1]["round_speaks"]["overall"]["mean"], Decimal("30.5"))

    def test_empty_and_interpolated_samples(self):
        self.assertEqual(summarize_sample([]), {"sample_size": 0, "p25": None, "p50": None, "p75": None})
        self.assertEqual(summarize_sample([Decimal(10), Decimal(20)])["p25"], Decimal("12.5"))
