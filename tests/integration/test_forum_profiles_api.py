"""Private forum lookup must honor OAuth identity and approved claims."""
import json

from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from core.models import Debater, School


@override_settings(FORUM_PROFILE_API_TOKEN="test-only-token")
class ForumProfilesTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="linked")
        self.account = SocialAccount.objects.create(user=self.user, provider="apdaonline", uid="42")
        self.school = School.objects.create(name="Example University")
        self.debater = Debater.objects.create(
            user=self.user, first_name="Alex", last_name="Example", school=self.school,
            email="private@example.com",
        )

    def lookup(self, wordpress_id=42, token="test-only-token"):
        return self.client.post(
            "/api/private/forum-profiles/", json.dumps({"wordpress_user_id": wordpress_id}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}",
        )

    def test_authentication_and_no_browser_session_bypass(self):
        self.client.force_login(self.user)
        for token in ("", "wrong"):
            response = self.lookup(token=token)
            self.assertEqual(response.status_code, 401)
            self.assertIn("no-store", response["Cache-Control"])
        with override_settings(FORUM_PROFILE_API_TOKEN=""):
            self.assertEqual(self.lookup().status_code, 401)

    def test_minimal_multiple_profiles(self):
        other = Debater.objects.create(user=self.user, first_name="Alex", last_name="Example")
        data = self.lookup().json()["debaters"]
        self.assertEqual({row["id"] for row in data}, {other.pk, self.debater.pk})
        self.assertEqual(next(row for row in data if row["id"] == self.debater.pk), {
            "id": self.debater.pk, "name": "Alex Example", "school": "Example University",
        })
        self.assertTrue(all(set(row) == {"id", "name", "school"} for row in data))

    def test_invalid_ids(self):
        for value in (True, 0, -1, "42", 4.2, None, []):
            self.assertEqual(self.lookup(value).status_code, 400)

    def test_identity_not_names_or_other_provider(self):
        self.assertEqual(self.lookup(43).json(), {"debaters": []})
        self.account.provider = "other"
        self.account.save()
        self.assertEqual(self.lookup().json(), {"debaters": []})

    def test_unlink_inactive_and_unclaimed(self):
        self.debater.user = None
        self.debater.save()
        self.assertEqual(self.lookup().json(), {"debaters": []})
        self.debater.user = self.user
        self.debater.save()
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.lookup().json(), {"debaters": []})
        self.user.is_active = True
        self.user.save()
        self.account.delete()
        self.assertEqual(self.lookup().json(), {"debaters": []})

    def test_nonpublic_records_excluded(self):
        for field in ("temporary", "synthetic"):
            Debater.objects.create(user=self.user, first_name="Hidden", **{field: True})
        self.assertEqual([row["id"] for row in self.lookup().json()["debaters"]], [self.debater.pk])
