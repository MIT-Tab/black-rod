import csv
from io import StringIO
from tempfile import TemporaryDirectory

from django.conf import settings
from django.contrib import admin as django_admin
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from core.models import GeneratedCode, User


class GeneratedCodeViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="code-user",
            password="testpass123",
            email="code-user@example.com",
        )
        self.url = reverse("core:generated_code")

    def test_generated_code_url_requires_login(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn(settings.LOGIN_URL, response.url)
        self.assertIn(self.url, response.url)

    def test_generated_code_url_is_not_present_in_nav(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("core:index"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.url)

    def test_post_generates_code_for_logged_in_user(self):
        self.client.force_login(self.user)

        response = self.client.post(self.url, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(GeneratedCode.objects.count(), 1)
        generated_code = GeneratedCode.objects.get()
        self.assertEqual(generated_code.user, self.user)
        self.assertEqual(len(generated_code.code), 12)
        self.assertContains(response, generated_code.code)


class GeneratedCodeModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="model-user",
            password="testpass123",
            email="model-user@example.com",
        )

    def test_manager_generates_unique_codes(self):
        first = GeneratedCode.objects.create_for_user(self.user)
        second = GeneratedCode.objects.create_for_user(self.user)

        self.assertNotEqual(first.code, second.code)

    def test_code_field_enforces_uniqueness(self):
        GeneratedCode.objects.create(user=self.user, code="ABC123XYZ789")

        with self.assertRaises(IntegrityError):
            GeneratedCode.objects.create(user=self.user, code="ABC123XYZ789")


class GeneratedCodeAdminTest(TestCase):
    def test_generated_code_is_not_registered_in_admin(self):
        self.assertNotIn(GeneratedCode, django_admin.site._registry)


class ExportGeneratedCodesCommandTest(TestCase):
    def setUp(self):
        self.first_user = User.objects.create_user(
            username="alpha",
            password="testpass123",
            email="alpha@example.com",
        )
        self.second_user = User.objects.create_user(
            username="bravo",
            password="testpass123",
            email="bravo@example.com",
        )
        GeneratedCode.objects.create(user=self.second_user, code="BBBB2222BBBB")
        GeneratedCode.objects.create(user=self.first_user, code="AAAA1111AAAA")

    def test_command_exports_csv_with_code_user_and_email(self):
        with TemporaryDirectory() as tmpdir:
            output_path = f"{tmpdir}/generated_codes.csv"
            stdout = StringIO()

            call_command(
                "export_generated_codes",
                "--output",
                output_path,
                stdout=stdout,
            )

            with open(output_path, newline="", encoding="utf-8") as csv_file:
                rows = list(csv.DictReader(csv_file))

        self.assertEqual(
            rows,
            [
                {
                    "code": "AAAA1111AAAA",
                    "username": "alpha",
                    "email": "alpha@example.com",
                },
                {
                    "code": "BBBB2222BBBB",
                    "username": "bravo",
                    "email": "bravo@example.com",
                },
            ],
        )
        self.assertIn("Exported 2 generated codes", stdout.getvalue())
