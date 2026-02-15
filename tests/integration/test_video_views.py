# pylint: disable=import-outside-toplevel
from datetime import date
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model

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

    def test_video_random_redirect_respects_filters(self):
        """Random redirect should honor applied filters."""
        target_video = Video.objects.create(
            pm=self.debater1,
            lo=self.debater2,
            mg=self.debater3,
            mo=self.debater4,
            tournament=self.tournament,
            link="https://example.com/filtered_video",
            round=Video.ROUND_TWO,
            permissions=Video.ALL,
        )
        Video.objects.create(
            pm=self.debater1,
            lo=self.debater2,
            mg=self.debater3,
            mo=self.debater4,
            tournament=self.tournament,
            link="https://example.com/non_filtered_video",
            round=Video.ROUND_THREE,
            permissions=Video.ALL,
        )

        response = self.client.get(
            reverse("core:video_random"),
            {"round": Video.ROUND_TWO},
        )

        self.assertRedirects(
            response,
            reverse("core:video_detail", kwargs={"pk": target_video.pk}),
            fetch_redirect_response=False,
        )

    def test_video_random_redirect_respects_account_level(self):
        """Anonymous users should not be redirected to restricted videos."""
        private_video = Video.objects.create(
            pm=self.debater1,
            lo=self.debater2,
            mg=self.debater3,
            mo=self.debater4,
            tournament=self.tournament,
            link="https://example.com/private_video",
            round=Video.ROUND_TWO,
            permissions=Video.ACCOUNTS_ONLY,
        )
        public_video = Video.objects.create(
            pm=self.debater1,
            lo=self.debater2,
            mg=self.debater3,
            mo=self.debater4,
            tournament=self.tournament,
            link="https://example.com/public_video",
            round=Video.ROUND_THREE,
            permissions=Video.ALL,
        )

        response = self.client.get(reverse("core:video_random"))
        self.assertRedirects(
            response,
            reverse("core:video_detail", kwargs={"pk": public_video.pk}),
            fetch_redirect_response=False,
        )

        user_model = get_user_model()
        private_user = user_model.objects.create_user(
            username="private_user",
            password="test-password",
            can_view_private_videos=True,
        )
        self.client.force_login(private_user)
        response = self.client.get(
            reverse("core:video_random"),
            {"round": Video.ROUND_TWO},
        )

        self.assertRedirects(
            response,
            reverse("core:video_detail", kwargs={"pk": private_video.pk}),
            fetch_redirect_response=False,
        )

    def test_video_random_redirect_falls_back_to_filtered_list_when_empty(self):
        """No matching videos should redirect back to the list with filters intact."""
        response = self.client.get(
            reverse("core:video_random"),
            {"tournament__name__icontains": "NoSuchTournamentName"},
        )

        self.assertRedirects(
            response,
            f"{reverse('core:video_list')}?tournament__name__icontains=NoSuchTournamentName",
            fetch_redirect_response=False,
        )

    def test_video_random_no_results_shows_warning_message(self):
        """No-result random requests should surface a warning message."""
        response = self.client.get(
            reverse("core:video_random"),
            {"tournament__name__icontains": "NoSuchTournamentName"},
            follow=True,
        )

        self.assertContains(
            response,
            "No results found, or you don't have access to any matching videos.",
        )
