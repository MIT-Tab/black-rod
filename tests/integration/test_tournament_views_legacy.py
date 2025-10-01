# pylint: disable=import-outside-toplevel
from datetime import date
from django.test import TestCase, Client
from django.urls import reverse

from core.models import School, Tournament, Debater, Team
from core.models.results.team import TeamResult
from core.models.results.speaker import SpeakerResult


class TournamentViewsTest(TestCase):
    """Test tournament views"""

    def setUp(self):
        self.client = Client()
        self.school = School.objects.create(name="Test School")
        self.tournament = Tournament.objects.create(
            manual_name="Test Tournament",
            host=self.school,
            date=date(2024, 1, 1),
            season="2024",
        )
        self.debater = Debater.objects.create(
            first_name="Test", last_name="Debater", school=self.school
        )
        self.team = Team.objects.create(name="Test Team")
        TeamResult.objects.create(
            tournament=self.tournament,
            team=self.team,
            place=1,
            type_of_place=Debater.VARSITY,
            ghost_points=False,
        )

    def test_tournament_list_view(self):
        """Test tournament list view"""
        response = self.client.get(reverse("core:tournament_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Tournament")

    def test_tournament_detail_view(self):
        """Test tournament detail view"""
        response = self.client.get(
            reverse("core:tournament_detail", kwargs={"pk": self.tournament.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Tournament")

    def test_tournament_view_with_results(self):
        """Test tournament view with actual results"""
        # Create a speaker result
        speaker_result = SpeakerResult.objects.create(
            debater=self.debater,
            tournament=self.tournament,
            type_of_place=Debater.VARSITY,
            place=1,
            tie=False,
        )

        response = self.client.get(
            reverse("core:tournament_detail", kwargs={"pk": self.tournament.pk})
        )
        self.assertEqual(response.status_code, 200)

    def test_nonexistent_tournament_404(self):
        """Test that non-existent tournament returns 404"""
        response = self.client.get(
            reverse("core:tournament_detail", kwargs={"pk": 99999})
        )
        self.assertEqual(response.status_code, 404)

    def test_tournament_search_view(self):
        """Test tournament search functionality"""
        response = self.client.get(reverse("core:tournament_list"), {"search": "Test"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Tournament")

    def test_tournament_filter_by_season(self):
        """Test filtering tournaments by season"""
        # Create another tournament in different season
        tournament_2025 = Tournament.objects.create(
            manual_name="2025 Tournament",
            host=self.school,
            date=date(2025, 1, 1),
            season="2025",
        )

        response = self.client.get(reverse("core:tournament_list"), {"season": "2024"})
        print(response.content)  # Debugging line to check response content
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Tournament")
        self.assertNotContains(response, "2025 Tournament")

    def test_tournament_with_qualifications(self):
        """Test tournament with qualification type"""
        qual_tournament = Tournament.objects.create(
            manual_name="Qualification Tournament",
            host=self.school,
            date=date(2024, 2, 1),
            season="2024",
        )

        response = self.client.get(
            reverse("core:tournament_detail", kwargs={"pk": qual_tournament.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Qualification Tournament")
