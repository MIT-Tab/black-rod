from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import Debater, Reaff, School, SpeakerResult, Team, TeamResult, Tournament
from core.utils.doty import get_doty_standings


class DOTYTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser(
            username="doty-admin", email="admin@example.com", password="password",
        )
        cls.school = School.objects.create(name="DOTY University", included_in_oty=True)
        cls.debater = Debater.objects.create(first_name="Alex", school=cls.school)
        cls.partner = Debater.objects.create(first_name="Blair", school=cls.school)
        cls.team = Team.objects.create(name="AB")
        cls.team.debaters.add(cls.debater, cls.partner)

    def tournament(self, season="2024", **kwargs):
        tournament = Tournament.objects.create(
            host=self.school, season=season, date=date(int(season), 10, 1),
            num_teams=80, **kwargs,
        )
        # Tournament.save derives these flags from its type; exercise each
        # component independently using the flags the calculation reads.
        flags = {key: value for key, value in kwargs.items() if key in ("soty", "qual")}
        if flags:
            Tournament.objects.filter(pk=tournament.pk).update(**flags)
            tournament.refresh_from_db()
        return tournament

    def speaker(self, tournament, debater=None, **kwargs):
        return SpeakerResult.objects.create(
            tournament=tournament, debater=debater or self.debater,
            **{"place": 1, **kwargs},
        )

    def team_result(self, tournament, team=None, **kwargs):
        return TeamResult.objects.create(
            tournament=tournament, team=team or self.team,
            **{"place": 1, **kwargs},
        )

    def test_example_combines_one_marker_and_includes_qual_only_partner(self):
        tournament = self.tournament()
        self.speaker(tournament)
        self.team_result(tournament)
        rows = get_doty_standings("2024")
        self.assertEqual([(row.debater, row.points) for row in rows], [
            (self.debater, 40), (self.partner, 20),
        ])
        self.assertEqual(len(rows[0].markers), 1)
        self.assertEqual(rows[0].markers[0].soty_points, 20)
        self.assertEqual(rows[0].markers[0].qual_points, 20)

    def test_best_six_are_selected_after_combining_components(self):
        for _ in range(6):
            self.speaker(self.tournament())
        combined = self.tournament()
        self.speaker(combined, place=5)  # 10 speaking + 20 qual beats a 20 marker.
        self.team_result(combined)
        row = get_doty_standings("2024")[0]
        self.assertEqual(row.points, 130)
        self.assertEqual(row.markers[0].tournament, combined)
        self.assertEqual(len(row.markers), 7)
        self.assertEqual(len(row.counting_markers), 6)

    def test_component_flags_ties_ghost_points_and_excluded_results(self):
        tied = self.tournament()
        self.speaker(tied, place=2, tie=True)
        self.team_result(tied, place=5, ghost_points=True)
        self.speaker(self.tournament(qual=False))
        self.team_result(self.tournament(soty=False))
        self.speaker(self.tournament(), counts_for_points=False)
        self.team_result(self.tournament(), counts_for_points=False)
        self.speaker(self.tournament(), type_of_place=Debater.NOVICE)
        self.team_result(self.tournament(), type_of_place=Debater.NOVICE)
        self.speaker(self.tournament(soty=False))
        self.team_result(self.tournament(qual=False))
        row = get_doty_standings("2024")[0]
        self.assertEqual(row.points, 69)  # (20 + 9) + 20 + 20
        self.assertEqual(len(row.markers), 3)

    def test_school_eligibility_zero_markers_and_speaker_only_results(self):
        tournament = self.tournament()
        self.speaker(tournament)
        self.team_result(tournament)
        no_school = Debater.objects.create(first_name="No school")
        excluded_school = School.objects.create(name="Excluded", included_in_oty=False)
        excluded = Debater.objects.create(first_name="Excluded", school=excluded_school)
        self.speaker(self.tournament(), debater=no_school)
        self.speaker(self.tournament(), debater=excluded)
        self.speaker(self.tournament(), place=100)
        rows = get_doty_standings("2024")
        self.assertEqual({row.debater for row in rows}, {self.debater, self.partner})
        self.assertEqual(len(rows[0].markers), 1)

    def test_reaffirmation_moves_both_components_to_new_profile_for_that_season(self):
        new = Debater.objects.create(first_name="Alex new", school=self.school)
        Reaff.objects.create(
            old_debater=self.debater, new_debater=new,
            season="2024", reaff_date=date(2024, 11, 1),
        )
        tournament = self.tournament()
        self.speaker(tournament)
        self.team_result(tournament)
        self.speaker(self.tournament(), debater=new)
        historical = self.tournament("2023")
        self.speaker(historical)
        rows = get_doty_standings("2024")
        self.assertEqual(rows[0].debater, new)
        self.assertEqual(rows[0].points, 60)
        self.assertNotIn(self.debater, [row.debater for row in rows])
        self.assertEqual(get_doty_standings("2023")[0].debater, self.debater)

    def test_competition_ranks_skip_places_after_a_tie(self):
        self.team_result(self.tournament())
        third = Debater.objects.create(first_name="Casey", school=self.school)
        self.speaker(self.tournament(), debater=third, place=2)
        self.assertEqual([(row.place, row.tied) for row in get_doty_standings("2024")],
                         [(1, True), (1, True), (3, False)])

    def test_admin_page_lists_full_season_and_never_writes(self):
        tournament = self.tournament()
        self.speaker(tournament)
        self.team_result(tournament)
        for index in range(12):
            debater = Debater.objects.create(first_name=f"Debater {index}", school=self.school)
            self.speaker(self.tournament(), debater=debater)
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("core:doty"), {"season": "2024"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["standings"]), 14)
        self.assertContains(response, "2024-25")
        self.assertContains(response, "40")
        self.assertContains(response, "20 + 20")
        self.assertContains(response, self.debater.get_absolute_url())
        self.assertContains(response, tournament.get_absolute_url())
        self.assertIn("no-store", response["Cache-Control"])
        for query in queries:
            self.assertTrue(query["sql"].lstrip().upper().startswith("SELECT"), query["sql"])
        self.assertContains(self.client.get(reverse("core:admin_tools")), reverse("core:doty"))

    @patch("core.views.doty_views.get_doty_standings")
    def test_anonymous_regular_and_staff_users_cannot_compute_preview(self, calculate):
        self.assertEqual(self.client.get(reverse("core:doty")).status_code, 302)
        for staff in (False, True):
            user = get_user_model().objects.create_user(username=f"user-{staff}", is_staff=staff)
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("core:doty")).status_code, 403)
        calculate.assert_not_called()

    @override_settings(CURRENT_SEASON="2024")
    def test_dynamic_seasons_defaults_invalid_selection_and_empty_season(self):
        self.speaker(self.tournament("2030"))
        self.speaker(self.tournament("2023"))
        self.client.force_login(self.admin)
        response = self.client.get(reverse("core:doty"))
        self.assertEqual(response.context["season"], "2030")
        self.assertEqual([option["value"] for option in response.context["seasons"]],
                         ["2030", "2023"])
        self.tournament()
        response = self.client.get(reverse("core:doty"), {"season": "invalid"})
        self.assertEqual(response.context["season"], "2024")
        self.assertContains(response, "No eligible DOTY results")
        response = self.client.get(reverse("core:doty"), {"season": "2023"})
        self.assertEqual(response.context["standings"][0].points, 20)

    def test_no_tournaments(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("core:doty")), "No eligible DOTY results")

    def test_query_count_does_not_grow_per_debater(self):
        self.team_result(self.tournament())
        with self.assertNumQueries(5):
            get_doty_standings("2024")
