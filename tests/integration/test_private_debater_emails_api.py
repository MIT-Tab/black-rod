import json

from django.test import Client, TestCase, override_settings

from core.models.debater import Debater
from core.models.school import School


class PrivateDebaterEmailsAPITest(TestCase):
    def setUp(self):
        self.client = Client()
        self.school = School.objects.create(name="Private Email U")
        self.debater = Debater.objects.create(
            first_name="Private",
            last_name="Email",
            school=self.school,
            email="private@example.com",
        )

    def test_requires_valid_bearer_token(self):
        response = self.client.post(
            "/api/private/debater-emails/",
            data=json.dumps({"debater_ids": [self.debater.pk]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)

    @override_settings(MITTAB_PRIVATE_API_TOKENS=["secret-token"])
    def test_returns_email_for_authorized_mittab_request(self):
        response = self.client.post(
            "/api/private/debater-emails/",
            data=json.dumps({"debater_ids": [self.debater.pk, 999999]}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer secret-token",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "debaters": [
                    {"id": self.debater.pk, "email": "private@example.com"},
                    {"id": 999999, "email": None},
                ]
            },
        )

    @override_settings(MITTAB_PRIVATE_API_TOKENS=["secret-token"])
    def test_rejects_non_integer_ids(self):
        response = self.client.post(
            "/api/private/debater-emails/",
            data=json.dumps({"debater_ids": ["not-an-id"]}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer secret-token",
        )

        self.assertEqual(response.status_code, 400)
