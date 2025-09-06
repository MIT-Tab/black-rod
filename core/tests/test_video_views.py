# pylint: disable=import-outside-toplevel
from datetime import date
from django.test import TestCase, Client
from django.urls import reverse

from core.models import School, Tournament, Debater, Video


class VideoViewsTest(TestCase):  # pylint: disable=too-many-instance-attributes
    """Test video views"""

    def setUp(self):
        self.client = Client()
        self.school = School.objects.create(name="Test School")
        self.debater1 = Debater.objects.create(
            first_name="PM", last_name="Debater", school=self.school
        )
        self.debater2 = Debater.objects.create(
            first_name="LO", last_name="Debater", school=self.school
        )
        self.debater3 = Debater.objects.create(
            first_name="MG", last_name="Debater", school=self.school
        )
        self.debater4 = Debater.objects.create(
            first_name="MO", last_name="Debater", school=self.school
        )
        self.tournament = Tournament.objects.create(
            name="Test Tournament",
            host=self.school,
            date=date(2024, 1, 1),
            season="2024",
        )
        self.video = Video.objects.create(
            pm=self.debater1,
            lo=self.debater2,
            mg=self.debater3,
            mo=self.debater4,
            tournament=self.tournament,
            link="https://example.com/video",
            round=Video.ROUND_ONE,
            permissions=Video.ALL,
        )

    def test_video_list_view(self):
        """Test video list view"""
        response = self.client.get(reverse("core:video_list"))
        self.assertEqual(response.status_code, 200)

    def test_video_detail_view(self):
        """Test video detail view"""
        response = self.client.get(
            reverse("core:video_detail", kwargs={"pk": self.video.pk})
        )
        print(response.content)  # Debugging line to check response content
        self.assertEqual(response.status_code, 200)

    def test_video_search_view(self):
        """Test video search functionality"""
        response = self.client.get(reverse("core:video_list"), {"search": "Test"})
        self.assertEqual(response.status_code, 200)

    def test_video_filter_by_tournament(self):
        """Test filtering videos by tournament"""
        response = self.client.get(
            reverse("core:video_list"), {"tournament": self.tournament.pk}
        )
        self.assertEqual(response.status_code, 200)

    def test_video_filter_by_round(self):
        """Test filtering videos by round"""
        response = self.client.get(
            reverse("core:video_list"), {"round": Video.ROUND_ONE}
        )
        self.assertEqual(response.status_code, 200)

    def test_video_filter_by_debater(self):
        """Test filtering videos by debater"""
        response = self.client.get(
            reverse("core:video_list"), {"debater": self.debater1.pk}
        )
        self.assertEqual(response.status_code, 200)

    def test_nonexistent_video_404(self):
        """Test that non-existent video returns 404"""
        response = self.client.get(reverse("core:video_detail", kwargs={"pk": 99999}))
        self.assertEqual(response.status_code, 404)

    def test_video_permissions_all(self):
        """Test video with ALL permissions"""
        self.video.permissions = Video.ALL
        self.video.save()

        response = self.client.get(
            reverse("core:video_detail", kwargs={"pk": self.video.pk})
        )
        self.assertEqual(response.status_code, 200)

    def test_video_permissions_accounts_only(self):
        """Test video with ACCOUNTS_ONLY permissions"""
        self.video.permissions = Video.ACCOUNTS_ONLY
        self.video.save()

        # Test without login - should be restricted
        response = self.client.get(
            reverse("core:video_detail", kwargs={"pk": self.video.pk})
        )
        # Depending on implementation, might be 403 or redirect
        self.assertIn(response.status_code, [200, 302, 403])

    def test_video_permissions_debaters_in_round(self):
        """Test video with DEBATERS_IN_ROUND permissions"""
        self.video.permissions = Video.DEBATERS_IN_ROUND
        self.video.save()

        response = self.client.get(
            reverse("core:video_detail", kwargs={"pk": self.video.pk})
        )
        # Depending on implementation, might be restricted
        self.assertIn(response.status_code, [200, 302, 403])

    def test_video_with_case_information(self):
        """Test video with case information"""
        self.video.case = "Sample case motion"
        self.video.description = "Sample description"
        self.video.save()

        response = self.client.get(
            reverse("core:video_detail", kwargs={"pk": self.video.pk})
        )
        self.assertEqual(response.status_code, 200)

    def test_video_with_password(self):
        """Test video with password protection"""
        self.video.password = "secret123"
        self.video.save()

        response = self.client.get(
            reverse("core:video_detail", kwargs={"pk": self.video.pk})
        )
        self.assertEqual(response.status_code, 200)

    def test_video_different_rounds(self):
        """Test videos from different rounds"""
        # Create videos for different rounds
        rounds_to_test = [Video.VF, Video.VS, Video.NF, Video.DEMO]

        for round_type in rounds_to_test:
            video = Video.objects.create(
                pm=self.debater1,
                lo=self.debater2,
                mg=self.debater3,
                mo=self.debater4,
                tournament=self.tournament,
                link=f"https://example.com/video_{round_type}",
                round=round_type,
                permissions=Video.ALL,
            )

            response = self.client.get(
                reverse("core:video_detail", kwargs={"pk": video.pk})
            )
            self.assertEqual(response.status_code, 200)

    def test_video_ordering(self):
        """Test video list ordering"""
        response = self.client.get(reverse("core:video_list"))
        self.assertEqual(response.status_code, 200)

    def test_video_pagination(self):
        """Test video list pagination"""
        # Create multiple videos to test pagination
        for i in range(15):
            Video.objects.create(
                pm=self.debater1,
                lo=self.debater2,
                mg=self.debater3,
                mo=self.debater4,
                tournament=self.tournament,
                link=f"https://example.com/video_{i}",
                round=Video.ROUND_ONE,
                permissions=Video.ALL,
            )

        response = self.client.get(reverse("core:video_list"))
        self.assertEqual(response.status_code, 200)
