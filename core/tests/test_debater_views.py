# pylint: disable=import-outside-toplevel
"""
Tests for debater views
"""


from datetime import date
from django.test import TestCase, Client
from django.urls import reverse

from core.models import School, Tournament, Debater
from core.models.results.speaker import SpeakerResult


class DebaterViewsTest(TestCase):
    """Test debater views"""

    def setUp(self):
        self.client = Client()
        self.school = School.objects.create(name="Test School")
        self.debater = Debater.objects.create(
            first_name="John", last_name="Doe", school=self.school
        )
        self.tournament = Tournament.objects.create(
            name="Test Tournament",
            host=self.school,
            date=date(2024, 1, 1),
            season="2024",
        )

    def test_debater_list_view(self):
        """Test debater list view"""
        response = self.client.get(reverse("core:debater_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "John")
        self.assertContains(response, "Doe")

    def test_debater_detail_view(self):
        """Test debater detail view"""
        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "John")
        self.assertContains(response, "Doe")

    def test_debater_with_results(self):
        """Test debater view with speaker results"""
        speaker_result = SpeakerResult.objects.create(
            debater=self.debater,
            tournament=self.tournament,
            type_of_place=Debater.VARSITY,
            place=5,
        )

        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "John")
        self.assertContains(response, "Doe")

    def test_debater_search_view(self):
        """Test debater search functionality"""
        response = self.client.get(reverse("core:debater_list"), {"search": "John"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "John")
        self.assertContains(response, "Doe")

    def test_debater_filter_by_school(self):
        """Test filtering debaters by school"""
        # Create another school and debater
        school2 = School.objects.create(name="Other School")
        debater2 = Debater.objects.create(
            first_name="Jane", last_name="Smith", school=school2
        )

        response = self.client.get(
            reverse("core:debater_list"), {"school": self.school.pk}
        )
        print(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "John")
        self.assertNotContains(response, "Jane")
        self.assertContains(response, "Doe")
        self.assertNotContains(response, "Smith")

    def test_nonexistent_debater_404(self):
        """Test that non-existent debater returns 404"""
        response = self.client.get(reverse("core:debater_detail", kwargs={"pk": 99999}))
        self.assertEqual(response.status_code, 404)

    def test_debater_statistics_view(self):
        """Test debater statistics display"""
        # Create multiple results for statistics
        for i in range(3):
            SpeakerResult.objects.create(
                debater=self.debater,
                tournament=self.tournament,
                type_of_place=Debater.VARSITY,
                place=i + 1,
            )

        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk})
        )
        self.assertEqual(response.status_code, 200)

    def test_debater_with_no_results(self):
        """Test debater view with no results"""
        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "John")
        self.assertContains(response, "Doe")
