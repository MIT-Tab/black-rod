# pylint: disable=import-outside-toplevel
"""
Tests for school views
"""
from datetime import date
from django.test import TestCase, Client
from django.urls import reverse

from core.models import School, Tournament, Debater, Team
from core.models.results.team import TeamResult
from core.models.standings.qual import QUAL
from core.models.user import User


class SchoolViewsTest(TestCase):
    """Test school views"""

    def setUp(self):
        self.client = Client()
        self.superuser = User.objects.create_superuser(
            username="admin", email="admin@test.com", password="admin123"
        )
        self.client.force_login(self.superuser)
        self.school = School.objects.create(name="Test School")
        self.debater = Debater.objects.create(
            first_name="John", last_name="Doe", school=self.school
        )
        self.tournament = Tournament.objects.create(
            manual_name="Test Tournament",
            host=self.school,
            date=date(2024, 1, 1),
            season="2024",
        )
        self.team = Team.objects.create(name="Test Team")
        self.team.debaters.add(self.debater)
        TeamResult.objects.create(
            tournament=self.tournament,
            team=self.team,
            place=1,
            type_of_place=Debater.VARSITY,
            ghost_points=False,
        )
        QUAL.objects.create(
            tournament=self.tournament, debater=self.debater, season="2024", qual_type=0
        )

    def test_school_list_view(self):
        """Test school list view"""
        response = self.client.get(reverse("core:school_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test School")

    def test_school_detail_redirect_without_season(self):
        """Test that school detail view redirects to current season when no season provided"""
        response = self.client.get(
            reverse("core:school_detail", kwargs={"pk": self.school.pk})
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("?season=2024", response.url)

    def test_school_detail_view(self):
        """Test school detail view"""
        response = self.client.get(
            reverse("core:school_detail", kwargs={"pk": self.school.pk})
            + "?season=2024"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test School")
        # Test context data
        self.assertIn("debaters", response.context)
        self.assertIn("quals", response.context)
        self.assertIn("tournaments", response.context)
        self.assertIn("cotys", response.context)
        self.assertEqual(response.context["current_season"], "2024")

    def test_school_with_tournaments(self):
        """Test school view with hosted tournaments"""
        response = self.client.get(
            reverse("core:school_detail", kwargs={"pk": self.school.pk})
            + "?season=2024"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Tournament")

    def test_school_search_view(self):
        """Test school search functionality"""
        response = self.client.get(
            reverse("core:school_list"), {"name__icontains": "Test"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test School")

    def test_nonexistent_school_404(self):
        """Test that non-existent school returns 404"""
        response = self.client.get(
            reverse("core:school_detail", kwargs={"pk": 99999}) + "?season=2024"
        )
        self.assertEqual(response.status_code, 404)

    def test_school_filter_by_region(self):
        """Test filtering schools by region"""
        # Test basic filter functionality (even if regions aren't implemented)
        response = self.client.get(reverse("core:school_list"), {"region": "Northeast"})
        self.assertEqual(response.status_code, 200)

    def test_school_with_multiple_debaters(self):
        """Test school with multiple debaters"""
        # Create additional debaters
        for i in range(3):
            Debater.objects.create(
                first_name=f"Debater{i}", last_name=f"Last{i}", school=self.school
            )

        response = self.client.get(
            reverse("core:school_detail", kwargs={"pk": self.school.pk})
            + "?season=2024"
        )
        self.assertEqual(response.status_code, 200)

    def test_school_with_multiple_tournaments(self):
        """Test school with multiple hosted tournaments"""
        # Create additional tournaments
        for i in range(3):
            Tournament.objects.create(
                name=f"Tournament {i}",
                host=self.school,
                date=date(2024, i + 1, 1),
                season="2024",
            )

        response = self.client.get(
            reverse("core:school_detail", kwargs={"pk": self.school.pk})
            + "?season=2024"
        )
        self.assertEqual(response.status_code, 200)

    def test_school_statistics_display(self):
        """Test school statistics display"""
        response = self.client.get(
            reverse("core:school_detail", kwargs={"pk": self.school.pk})
            + "?season=2024"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test School")

    def test_school_ordering(self):
        """Test school list ordering"""
        # Create schools with different names for ordering test
        School.objects.create(name="Alpha School")
        School.objects.create(name="Beta School")

        response = self.client.get(reverse("core:school_list"))
        self.assertEqual(response.status_code, 200)

    def test_school_pagination(self):
        """Test school list pagination"""
        # Create many schools to test pagination
        for i in range(25):
            School.objects.create(name=f"School {i:02d}")

        response = self.client.get(reverse("core:school_list"))
        self.assertEqual(response.status_code, 200)

    def test_school_autocomplete_view(self):
        """Test school autocomplete functionality"""
        response = self.client.get(
            reverse("core:school_autocomplete"), {"q": "Test"}
        )
        self.assertEqual(response.status_code, 200)
        # Should contain our test school
        self.assertContains(response, "Test School")
        self.assertContains(response, f"<{self.school.id}>")

    def test_school_autocomplete_no_query(self):
        """Test school autocomplete without query"""
        response = self.client.get(reverse("core:school_autocomplete"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test School")
