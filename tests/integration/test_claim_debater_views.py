from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from core.models import Debater, School


class ClaimDebaterViewsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = get_user_model().objects.create_user(
            username="claim-user",
            password="testpass123",
            email="claim-user@example.com",
        )
        self.client.force_login(self.user)

        self.school = School.objects.create(name="Claim School")
        self.synthetic_school = School.objects.create(
            name="Synthetic Claim School",
            synthetic=True,
        )
        self.debater = Debater.objects.create(
            first_name="Claim",
            last_name="Target",
            school=self.school,
        )
        self.synthetic_debater = Debater.all_objects.create(
            first_name="Synthetic",
            last_name="Target",
            school=self.school,
            synthetic=True,
        )

    def test_claim_form_queryset_excludes_synthetic_schools_and_debaters(self):
        response = self.client.get(reverse("core:claim_debater_request_create"))

        self.assertEqual(response.status_code, 200)
        school_ids = set(response.context["form"].fields["school"].queryset.values_list("id", flat=True))
        debater_ids = set(response.context["form"].fields["debater"].queryset.values_list("id", flat=True))

        self.assertIn(self.school.id, school_ids)
        self.assertNotIn(self.synthetic_school.id, school_ids)
        self.assertIn(self.debater.id, debater_ids)
        self.assertNotIn(self.synthetic_debater.id, debater_ids)

    def test_claim_school_autocomplete_hides_synthetic_schools_by_default(self):
        response = self.client.get(
            reverse("core:claim_school_autocomplete"),
            {"q": "Claim"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.school.name)
        self.assertNotContains(response, self.synthetic_school.name)

    def test_claim_debater_autocomplete_hides_synthetic_debaters_by_default(self):
        response = self.client.get(
            reverse("core:debater_autocomplete"),
            {"q": "Target"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.debater.name)
        self.assertNotContains(response, self.synthetic_debater.name)
