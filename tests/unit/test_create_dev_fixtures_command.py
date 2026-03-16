import json
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase

from core.models import ClaimDebaterRequest, Debater, School, User


class CreateDevFixturesCommandTest(TestCase):
    def test_command_rewrites_user_foreign_keys_to_sanitized_usernames(self):
        requester = User.objects.create_user(
            username="requester",
            email="requester@example.com",
            password="secret",
        )
        reviewer = User.objects.create_user(
            username="reviewer",
            email="reviewer@example.com",
            password="secret",
        )
        school = School.objects.create(name="Fixture School", short_name="FS")
        debater = Debater.objects.create(
            first_name="Casey",
            last_name="Example",
            school=school,
            user=requester,
        )
        claim_request = ClaimDebaterRequest.objects.create(
            requested_by=requester,
            debater=debater,
            status=ClaimDebaterRequest.STATUS_DENIED,
            processed_by=reviewer,
            denial_reason="Needs more info",
        )

        with TemporaryDirectory() as tmpdir:
            output_path = f"{tmpdir}/dev_fixtures.json"
            call_command("create_dev_fixtures", "--output", output_path)

            with open(output_path, encoding="utf-8") as fixture_file:
                payload = json.load(fixture_file)

        claim_request_item = next(
            item
            for item in payload
            if item["model"] == "core.claimdebaterrequest" and item["pk"] == claim_request.pk
        )
        claim_request_index = next(
            index
            for index, item in enumerate(payload)
            if item["model"] == "core.claimdebaterrequest" and item["pk"] == claim_request.pk
        )
        debater_item = next(
            item
            for item in payload
            if item["model"] == "core.debater" and item["pk"] == debater.pk
        )
        first_user_index = next(
            index
            for index, item in enumerate(payload)
            if item["model"] == "core.user"
        )
        user_items = [item for item in payload if item["model"] == "core.user"]

        self.assertEqual(
            claim_request_item["fields"]["requested_by"],
            [f"user_{requester.id}"],
        )
        self.assertEqual(
            claim_request_item["fields"]["processed_by"],
            [f"user_{reviewer.id}"],
        )
        self.assertEqual(debater_item["fields"]["user"], [f"user_{requester.id}"])
        self.assertLess(first_user_index, claim_request_index)
        self.assertCountEqual(
            [item["fields"]["username"] for item in user_items],
            [f"user_{requester.id}", f"user_{reviewer.id}"],
        )
