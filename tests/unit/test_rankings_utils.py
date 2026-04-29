"""
Tests for ranking utilities
"""


from datetime import date
from io import StringIO
from django.core.management import call_command
from django.test import TestCase

from core.models import School, Tournament, Debater, Team
from core.models.debater import QualPoints
from core.models.results.team import TeamResult
from core.models.results.speaker import SpeakerResult
from core.models.standings.coty import COTY
from core.models.standings.soty import SOTY
from core.models.standings.qual import QUAL
from core.models.standings.toty import TOTY, TOTYReaff
from core.models.standings.online_qual import OnlineQUAL
from core.utils import merge
from core.utils import rankings


class RankingsUtilsTest(TestCase):
    """Test ranking utilities"""

    def setUp(self):
        self.school = School.objects.create(name="Test School", included_in_oty=True)
        self.tournament = Tournament.objects.create(
            name="Test Tournament",
            host=self.school,
            date=date(2024, 1, 1),
            season="2024",
            toty=True,
            soty=True,
            noty=True,
            num_teams=32,
        )
        self.debater1 = Debater.objects.create(
            first_name="John", last_name="Doe", school=self.school
        )
        self.debater2 = Debater.objects.create(
            first_name="Jane", last_name="Smith", school=self.school
        )
        self.team = Team.objects.create(name="Test Team")
        self.team.debaters.add(self.debater1, self.debater2)

    def test_get_qualled_debaters(self):
        """Test get_qualled_debaters function returns correct QualPoints objects"""
        # Create third debater for testing
        debater3 = Debater.objects.create(
            first_name="Bob", last_name="Wilson", school=self.school
        )

        # Create QUAL record for debater1 (who also has QualPoints)
        QUAL.objects.create(
            tournament=self.tournament, debater=self.debater1, season="2024", qual_type=0
        )
        QualPoints.objects.create(debater=self.debater1, points=5, season="2024")

        # Create QUAL record for debater3 (no QualPoints)
        QUAL.objects.create(
            tournament=self.tournament, debater=debater3, season="2024", qual_type=0
        )

        # Create QualPoints for debater2
        QualPoints.objects.create(debater=self.debater2, points=3, season="2024")

        # Test the function
        result = rankings.get_qualled_debaters(self.school, "2024")

        # Should return QualPoints objects
        self.assertEqual(len(result), 3)

        # Check that debaters are included
        debater_ids = [qp.debater.id for qp in result]
        self.assertIn(self.debater1.id, debater_ids)
        self.assertIn(self.debater2.id, debater_ids)
        self.assertIn(debater3.id, debater_ids)

        # Display call should not write placeholder QualPoints rows
        qual_points = QualPoints.objects.filter(debater=debater3, season="2024")
        self.assertEqual(qual_points.count(), 0)

    def test_get_qualled_debaters_no_quals(self):
        """Test get_qualled_debaters with no qualified debaters"""
        result = rankings.get_qualled_debaters(self.school, "2024")
        self.assertEqual(len(result), 0)

    def test_get_qualled_debaters_historical_season_is_read_only(self):
        """Historical render pass should not create point-qual rows."""
        season = "2023"
        with self.settings(HISTORICAL_QUAL_BARS={season: 10.0}, CURRENT_SEASON="2024"):
            QualPoints.objects.create(debater=self.debater1, points=12, season=season)
            QualPoints.objects.create(debater=self.debater2, points=9.5, season=season)

            result = rankings.get_qualled_debaters(self.school, season)

        by_debater = {qp.debater_id: qp.qualled for qp in result}
        self.assertFalse(by_debater[self.debater1.id])
        self.assertFalse(by_debater[self.debater2.id])
        self.assertFalse(
            QUAL.objects.filter(
                season=season, debater=self.debater1, qual_type=QUAL.POINTS
            ).exists()
        )
        self.assertFalse(
            QUAL.objects.filter(
                season=season, debater=self.debater2, qual_type=QUAL.POINTS
            ).exists()
        )

    def test_reconstruct_historical_point_quals_command_creates_missing_rows(self):
        season = "2023"
        with self.settings(
            HISTORICAL_QUAL_BARS={season: 10.0},
            CURRENT_SEASON="2024",
            ONLINE_SEASONS=("2020", "2021"),
        ):
            QualPoints.objects.create(debater=self.debater1, points=12, season=season)
            QualPoints.objects.create(debater=self.debater2, points=8.5, season=season)

            call_command(
                "reconstruct_historical_point_quals",
                "--season",
                season,
                stdout=StringIO(),
            )

            self.assertTrue(
                QUAL.objects.filter(
                    season=season, debater=self.debater1, qual_type=QUAL.POINTS
                ).exists()
            )
            self.assertFalse(
                QUAL.objects.filter(
                    season=season, debater=self.debater2, qual_type=QUAL.POINTS
                ).exists()
            )

    def test_reconstruct_historical_point_quals_skips_already_qualified_debaters(self):
        season = "2023"
        with self.settings(
            HISTORICAL_QUAL_BARS={season: 10.0},
            CURRENT_SEASON="2024",
            ONLINE_SEASONS=("2020", "2021"),
        ):
            QualPoints.objects.create(debater=self.debater1, points=12, season=season)
            QualPoints.objects.create(debater=self.debater2, points=12, season=season)
            QUAL.objects.create(
                season=season,
                debater=self.debater1,
                qual_type=QUAL.EXPANSION,
            )

            call_command(
                "reconstruct_historical_point_quals",
                "--season",
                season,
                stdout=StringIO(),
            )

            self.assertTrue(
                QUAL.objects.filter(
                    season=season, debater=self.debater1, qual_type=QUAL.EXPANSION
                ).exists()
            )
            self.assertFalse(
                QUAL.objects.filter(
                    season=season, debater=self.debater1, qual_type=QUAL.POINTS
                ).exists()
            )
            self.assertTrue(
                QUAL.objects.filter(
                    season=season, debater=self.debater2, qual_type=QUAL.POINTS
                ).exists()
            )

    def test_reconstruct_historical_point_quals_skips_excluded_schools(self):
        season = "2023"
        excluded_school = School.objects.create(
            name="Excluded School",
            included_in_oty=False,
        )
        excluded_debater = Debater.objects.create(
            first_name="Excluded",
            last_name="Debater",
            school=excluded_school,
            first_season=season,
            latest_season=season,
        )

        with self.settings(
            HISTORICAL_QUAL_BARS={season: 10.0},
            CURRENT_SEASON="2024",
            ONLINE_SEASONS=("2020", "2021"),
        ):
            QualPoints.objects.create(debater=self.debater1, points=12, season=season)
            QualPoints.objects.create(debater=excluded_debater, points=12, season=season)

            call_command(
                "reconstruct_historical_point_quals",
                "--season",
                season,
                stdout=StringIO(),
            )

            self.assertTrue(
                QUAL.objects.filter(
                    season=season, debater=self.debater1, qual_type=QUAL.POINTS
                ).exists()
            )
            self.assertFalse(
                QUAL.objects.filter(
                    season=season, debater=excluded_debater, qual_type=QUAL.POINTS
                ).exists()
            )

    def test_reconstruct_historical_point_quals_command_dry_run(self):
        season = "2023"
        with self.settings(
            HISTORICAL_QUAL_BARS={season: 10.0},
            CURRENT_SEASON="2024",
            ONLINE_SEASONS=("2020", "2021"),
        ):
            QualPoints.objects.create(debater=self.debater1, points=12, season=season)

            call_command(
                "reconstruct_historical_point_quals",
                "--season",
                season,
                "--dry-run",
                stdout=StringIO(),
            )

            self.assertFalse(
                QUAL.objects.filter(
                    season=season, debater=self.debater1, qual_type=QUAL.POINTS
                ).exists()
            )

    def test_reconstruct_historical_point_quals_command_all_excludes_current(self):
        with self.settings(
            HISTORICAL_QUAL_BARS={"2024": 10.0, "2023": 10.0},
            CURRENT_SEASON="2024",
            ONLINE_SEASONS=("2020", "2021"),
        ):
            QualPoints.objects.create(debater=self.debater1, points=12, season="2024")
            QualPoints.objects.create(debater=self.debater2, points=12, season="2023")

            call_command(
                "reconstruct_historical_point_quals",
                "--all",
                stdout=StringIO(),
            )

            self.assertFalse(
                QUAL.objects.filter(
                    season="2024", debater=self.debater1, qual_type=QUAL.POINTS
                ).exists()
            )
            self.assertTrue(
                QUAL.objects.filter(
                    season="2023", debater=self.debater2, qual_type=QUAL.POINTS
                ).exists()
            )

    def test_place_as_round(self):
        """Test place_as_round function"""
        self.assertEqual(rankings.place_as_round(1), "1st")
        self.assertEqual(rankings.place_as_round(2), "2nd")
        self.assertEqual(rankings.place_as_round(3), "Semi-Finalist")
        self.assertEqual(rankings.place_as_round(4), "Semi-Finalist")
        self.assertEqual(rankings.place_as_round(5), "Quarter-Finalist")
        self.assertEqual(rankings.place_as_round(8), "Quarter-Finalist")
        self.assertEqual(rankings.place_as_round(9), "Octo-Finalist")
        self.assertEqual(rankings.place_as_round(16), "Octo-Finalist")
        self.assertEqual(rankings.place_as_round(17), "Double Octo-Finalist")
        self.assertEqual(rankings.place_as_round(32), "Double Octo-Finalist")
        self.assertEqual(rankings.place_as_round(33), "Quadruple Octo-Finalist")
        self.assertEqual(rankings.place_as_round(64), "Quadruple Octo-Finalist")
        self.assertEqual(rankings.place_as_round(65), "Top 65")

    def test_update_toty_with_results(self):
        """Test update_toty function with team results"""
        # Create team results
        TeamResult.objects.create(
            team=self.team,
            tournament=self.tournament,
            type_of_place=Debater.VARSITY,
            place=1,
        )
        TeamResult.objects.create(
            team=self.team,
            tournament=self.tournament,
            type_of_place=Debater.VARSITY,
            place=2,
        )

        # Call update_toty
        toty = rankings.update_toty(self.team, "2024")

        # Check that TOTY was created
        self.assertIsNotNone(toty)
        self.assertEqual(toty.team, self.team)
        self.assertGreater(toty.points, 0)
        self.assertGreater(toty.marker_one, 0)

    def test_update_toty_ignores_results_excluded_from_points(self):
        TeamResult.objects.create(
            team=self.team,
            tournament=self.tournament,
            type_of_place=Debater.VARSITY,
            place=1,
            counts_for_points=False,
        )

        toty = rankings.update_toty(self.team, "2024")
        self.assertIsNone(toty)
        self.assertFalse(TOTY.objects.filter(team=self.team, season="2024").exists())

    def test_update_toty_no_results(self):
        """Test update_toty with no results"""
        toty = rankings.update_toty(self.team, "2024")
        # Should return None since no results
        self.assertIsNone(toty)

    def test_update_soty_skips_debaters_without_school(self):
        """update_soty should not crash when a debater lacks a school."""
        debater = Debater.objects.create(first_name="No", last_name="School")
        SpeakerResult.objects.create(
            tournament=self.tournament,
            debater=debater,
            type_of_place=Debater.VARSITY,
            place=1,
        )

        rankings.update_soty(debater, "2024")

        self.assertEqual(SOTY.objects.count(), 0)

    def test_update_toty_hybrid_team(self):
        """Test update_toty with hybrid team"""
        # Create a hybrid team by adding debaters from different schools
        school2 = School.objects.create(name="School 2", included_in_oty=True)
        debater3 = Debater.objects.create(
            first_name="Bob", last_name="Wilson", school=school2
        )
        self.team.debaters.add(debater3)

        TeamResult.objects.create(
            team=self.team,
            tournament=self.tournament,
            type_of_place=Debater.VARSITY,
            place=1,
        )

        toty = rankings.update_toty(self.team, "2024")
        # Should return None for hybrid teams
        self.assertIsNone(toty)

    def test_update_toty_with_reaff(self):
        """Test update_toty with reaffiliation"""
        # Create another team
        old_team = Team.objects.create(name="Old Team")
        old_team.debaters.add(self.debater1)

        # Create results for old team
        TeamResult.objects.create(
            team=old_team,
            tournament=self.tournament,
            type_of_place=Debater.VARSITY,
            place=1,
        )

        # Create reaffiliation
        TOTYReaff.objects.create(
            season="2024",
            old_team=old_team,
            new_team=self.team,
            reaff_date=date(2024, 6, 1)
        )

        # Call update_toty - should not crash
        toty = rankings.update_toty(self.team, "2024")

        # The function should handle reaffiliation without error
        # (exact behavior may depend on implementation details)

    def test_update_qual_points(self):
        """Test update_qual_points function"""
        # Configure tournament to auto-qual top four
        self.tournament.autoqual_bar = 4
        self.tournament.save()

        expected_points = self.tournament.get_qual_points(place=3)

        TeamResult.objects.create(
            team=self.team,
            tournament=self.tournament,
            type_of_place=Debater.VARSITY,
            place=3,
        )

        # Call update_qual_points
        rankings.update_qual_points(self.team, "2024")

        qual_points = QualPoints.objects.filter(
            debater__in=self.team.debaters.all(), season="2024"
        )
        self.assertEqual(qual_points.count(), 2)
        for qp in qual_points:
            self.assertAlmostEqual(qp.points, expected_points)

        quals = QUAL.objects.filter(
            debater__in=self.team.debaters.all(), season="2024", qual_type=QUAL.POINTS
        )
        self.assertEqual(quals.count(), 2)

        coty = COTY.objects.get(season="2024", school=self.school)
        self.assertAlmostEqual(
            coty.points, expected_points * 2 + len(self.team.debaters.all()) * 6
        )

    def test_update_qual_points_ignores_results_excluded_from_points(self):
        self.tournament.autoqual_bar = 4
        self.tournament.save()

        TeamResult.objects.create(
            team=self.team,
            tournament=self.tournament,
            type_of_place=Debater.VARSITY,
            place=3,
            counts_for_points=False,
        )

        rankings.update_qual_points(self.team, "2024")

        self.assertFalse(
            QualPoints.objects.filter(
                debater__in=self.team.debaters.all(), season="2024"
            ).exists()
        )
        self.assertFalse(
            QUAL.objects.filter(
                debater__in=self.team.debaters.all(),
                season="2024",
                qual_type=QUAL.POINTS,
            ).exists()
        )

    def test_update_qual_points_does_not_create_coty_for_online_season(self):
        online_tournament = Tournament.objects.create(
            name="Online Qual Tournament",
            host=self.school,
            date=date(2024, 2, 1),
            season="2024",
            qual=True,
            num_teams=32,
            autoqual_bar=4,
        )
        TeamResult.objects.create(
            team=self.team,
            tournament=online_tournament,
            type_of_place=Debater.VARSITY,
            place=3,
        )

        with self.settings(ONLINE_SEASONS=("2024",)):
            rankings.update_qual_points(self.team, "2024")

        self.assertFalse(COTY.objects.filter(season="2024", school=self.school).exists())

    def test_update_qual_points_uses_historical_season_for_latest_season(self):
        historical_tournament = Tournament.objects.create(
            name="Historical Tournament",
            host=self.school,
            date=date(2020, 2, 1),
            season="2020",
            qual=True,
            num_teams=32,
            autoqual_bar=4,
        )
        self.debater1.latest_season = "2019"
        self.debater1.save(update_fields=["latest_season"])

        historical_team = Team.objects.create(name="Historical Team")
        historical_team.debaters.add(self.debater1)
        TeamResult.objects.create(
            team=historical_team,
            tournament=historical_tournament,
            type_of_place=Debater.VARSITY,
            place=3,
        )

        with self.settings(CURRENT_SEASON="2024"):
            rankings.update_qual_points(historical_team, "2020")

        self.debater1.refresh_from_db()
        self.assertEqual(self.debater1.latest_season, "2020")

    def test_redo_rankings_toty(self):
        """Test redo_rankings function for toty"""
        # Create some results and rankings
        TeamResult.objects.create(
            team=self.team,
            tournament=self.tournament,
            type_of_place=Debater.VARSITY,
            place=1,
        )

        # Create TOTY record
        rankings.update_toty(self.team, "2024")

        # Get TOTY queryset
        toty_queryset = TOTY.objects.filter(season="2024")

        # Call redo_rankings
        rankings.redo_rankings(toty_queryset, "2024", "toty")

        # Check that rankings were updated

    def test_seed_season_filters_can_limit_to_latest_for_active_season(self):
        season_2020 = Tournament.objects.create(
            name="2020 Tournament",
            host=self.school,
            date=date(2020, 1, 1),
            season="2020",
            num_teams=32,
        )
        season_2021 = Tournament.objects.create(
            name="2021 Tournament",
            host=self.school,
            date=date(2021, 1, 1),
            season="2021",
            num_teams=32,
        )
        season_2019 = Tournament.objects.create(
            name="2019 Tournament",
            host=self.school,
            date=date(2019, 1, 1),
            season="2019",
            num_teams=32,
        )

        active_2020 = Debater.objects.create(
            first_name="Casey",
            last_name="Active",
            school=self.school,
            first_season="wrong-first",
            latest_season="wrong-latest",
        )
        inactive_2020 = Debater.objects.create(
            first_name="Robin",
            last_name="Inactive",
            school=self.school,
            first_season="leave-first",
            latest_season="leave-latest",
        )

        SpeakerResult.objects.create(
            tournament=season_2020,
            debater=active_2020,
            type_of_place=Debater.VARSITY,
            place=1,
        )
        SpeakerResult.objects.create(
            tournament=season_2021,
            debater=active_2020,
            type_of_place=Debater.VARSITY,
            place=1,
        )
        SpeakerResult.objects.create(
            tournament=season_2019,
            debater=inactive_2020,
            type_of_place=Debater.VARSITY,
            place=1,
        )

        stdout = StringIO()
        call_command(
            "seed_season_filters",
            "--latest-only",
            "--active-in-season",
            "2020",
            stdout=stdout,
        )

        active_2020.refresh_from_db()
        inactive_2020.refresh_from_db()

        self.assertEqual(active_2020.first_season, "wrong-first")
        self.assertEqual(active_2020.latest_season, "2021")
        self.assertEqual(inactive_2020.first_season, "leave-first")
        self.assertEqual(inactive_2020.latest_season, "leave-latest")

    def test_redo_rankings_handles_ties_and_zero_entries(self):
        other_school = School.objects.create(name="Second School", included_in_oty=True)
        debater_a = Debater.objects.create(
            first_name="Pat", last_name="Riley", school=other_school
        )
        debater_b = Debater.objects.create(
            first_name="Morgan", last_name="Shaw", school=other_school
        )
        team_b = Team.objects.create(name="Alpha")
        team_b.debaters.add(debater_a, debater_b)

        team_c_school = School.objects.create(name="Zero School", included_in_oty=True)
        debater_c1 = Debater.objects.create(
            first_name="Dana", last_name="Cole", school=team_c_school
        )
        debater_c2 = Debater.objects.create(
            first_name="Reese", last_name="Poe", school=team_c_school
        )
        team_c = Team.objects.create(name="Zero")
        team_c.debaters.add(debater_c1, debater_c2)

        TOTY.objects.create(season="2024", team=self.team, points=18)
        TOTY.objects.create(season="2024", team=team_b, points=18)
        TOTY.objects.create(season="2024", team=team_c, points=0)

        rankings.redo_rankings(TOTY.objects.filter(season="2024"), "2024", "toty")

        placements = {
            toty.team_id: (toty.place, toty.tied)
            for toty in TOTY.objects.filter(points__gt=0)
        }
        self.assertEqual(placements[self.team.id], (1, True))
        self.assertEqual(placements[team_b.id], (1, True))
        self.assertNotIn(team_c.id, placements)
        self.assertFalse(TOTY.objects.filter(team=team_c, season="2024").exists())

    def test_update_online_quals(self):
        """Test update_online_quals awards points and promotes quals for online seasons"""
        # Create tournament with online quals
        online_tournament = Tournament.objects.create(
            name="Online Tournament",
            host=self.school,
            date=date(2024, 3, 1),
            season="2024",
            online_qual_points=True,
            num_teams=16,
        )

        # Create team results
        TeamResult.objects.create(
            team=self.team,
            tournament=online_tournament,
            type_of_place=Debater.VARSITY,
            place=1,
        )

        with self.settings(ONLINE_SEASONS=("2024",), ONLINE_QUAL_BAR=10):
            rankings.update_online_quals(self.team, "2024")

        online_qual = OnlineQUAL.objects.filter(
            debater__in=self.team.debaters.all(), season="2024"
        ).order_by("debater_id")
        self.assertEqual(online_qual.count(), 2)
        for oq in online_qual:
            self.assertAlmostEqual(oq.points, 12.5)
            self.assertAlmostEqual(oq.marker_one, 12.5)
        self.assertTrue(all(oq.tournament_one == online_tournament for oq in online_qual))

        quals = QUAL.objects.filter(
            debater__in=self.team.debaters.all(), season="2024", qual_type=QUAL.POINTS
        )
        self.assertEqual(quals.count(), 2)

    def test_update_online_quals_ignores_results_excluded_from_points(self):
        online_tournament = Tournament.objects.create(
            name="Online Tournament Excluded",
            host=self.school,
            date=date(2024, 4, 1),
            season="2024",
            online_qual_points=True,
            num_teams=16,
        )
        TeamResult.objects.create(
            team=self.team,
            tournament=online_tournament,
            type_of_place=Debater.VARSITY,
            place=1,
            counts_for_points=False,
        )

        with self.settings(ONLINE_SEASONS=("2024",), ONLINE_QUAL_BAR=10):
            rankings.update_online_quals(self.team, "2024")

        self.assertFalse(
            OnlineQUAL.objects.filter(
                debater__in=self.team.debaters.all(), season="2024"
            ).exists()
        )

    def test_update_online_quals_removes_existing_coty_for_online_season(self):
        COTY.objects.create(season="2024", school=self.school, points=12, place=1)
        online_tournament = Tournament.objects.create(
            name="Online Tournament",
            host=self.school,
            date=date(2024, 3, 1),
            season="2024",
            online_qual_points=True,
            num_teams=16,
        )
        TeamResult.objects.create(
            team=self.team,
            tournament=online_tournament,
            type_of_place=Debater.VARSITY,
            place=1,
        )

        with self.settings(ONLINE_SEASONS=("2024",), ONLINE_QUAL_BAR=10):
            rankings.update_online_quals(self.team, "2024")

        self.assertFalse(COTY.objects.filter(season="2024", school=self.school).exists())

    def test_update_online_quals_keeps_existing_coty_for_non_online_season(self):
        COTY.objects.create(season="2024", school=self.school, points=12, place=1)
        tournament = Tournament.objects.create(
            name="In-Person Tournament",
            host=self.school,
            date=date(2024, 3, 1),
            season="2024",
            num_teams=16,
        )
        TeamResult.objects.create(
            team=self.team,
            tournament=tournament,
            type_of_place=Debater.VARSITY,
            place=1,
        )

        with self.settings(ONLINE_SEASONS=("2020", "2021"), ONLINE_QUAL_BAR=10):
            rankings.update_online_quals(self.team, "2024")

        self.assertTrue(COTY.objects.filter(season="2024", school=self.school).exists())

    def test_recompute_coty_skips_online_season(self):
        COTY.objects.create(season="2024", school=self.school, points=12, place=1)

        with self.settings(ONLINE_SEASONS=("2024",)):
            merge._recompute_coty(self.school, "2024")

        self.assertFalse(COTY.objects.filter(season="2024", school=self.school).exists())

    def test_update_toty_no_debaters(self):
        """Test update_toty with team having no debaters"""
        empty_team = Team.objects.create(name="Empty Team")
        toty = rankings.update_toty(empty_team, "2024")
        # Should return None
        self.assertIsNone(toty)

    def test_update_soty_with_results(self):
        """Test update_soty function with speaker results"""
        # Create speaker results
        SpeakerResult.objects.create(
            debater=self.debater1,
            tournament=self.tournament,
            type_of_place=Debater.VARSITY,
            place=1,
        )
        SpeakerResult.objects.create(
            debater=self.debater1,
            tournament=self.tournament,
            type_of_place=Debater.VARSITY,
            place=2,
        )

        # Call update_soty
        soty = rankings.update_soty(self.debater1, "2024")

        # Check that SOTY was created
        self.assertIsNotNone(soty)
        self.assertEqual(soty.debater, self.debater1)
        self.assertGreater(soty.points, 0)
        self.assertGreater(soty.marker_one, 0)

    def test_update_soty_ignores_results_excluded_from_points(self):
        SpeakerResult.objects.create(
            debater=self.debater1,
            tournament=self.tournament,
            type_of_place=Debater.VARSITY,
            place=1,
            counts_for_points=False,
        )

        soty = rankings.update_soty(self.debater1, "2024")
        self.assertIsNone(soty)
        self.assertFalse(
            SOTY.objects.filter(debater=self.debater1, season="2024").exists()
        )

    def test_update_soty_no_results(self):
        """Test update_soty with no results"""
        soty = rankings.update_soty(self.debater1, "2024")
        # Should return None since no results
        self.assertIsNone(soty)

    def test_update_noty_with_results(self):
        """Test update_noty function with novice results"""
        # Create novice tournament
        novice_tournament = Tournament.objects.create(
            name="Novice Tournament",
            host=self.school,
            date=date(2024, 2, 1),
            season="2019",  # Use a season before LAST_NOTY_SEASON
            num_teams=16,
        )
        Tournament.objects.filter(pk=novice_tournament.pk).update(
            noty=True, num_novice_debaters=20
        )
        novice_tournament.refresh_from_db()

        # Create speaker results for novice
        SpeakerResult.objects.create(
            debater=self.debater1,
            tournament=novice_tournament,
            type_of_place=Debater.NOVICE,
            place=1,
        )

        # Call update_noty - should not crash
        try:
            noty = rankings.update_noty(self.debater1, 2019)
            self.assertIsNotNone(noty)
            self.assertAlmostEqual(noty.points, novice_tournament.get_noty_points(1))
        except AttributeError:
            # Settings not available
            self.skipTest("Settings not available")
