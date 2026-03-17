import json
from tempfile import TemporaryDirectory

from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import TestCase

from core.models import ClaimDebaterRequest, Debater, School, SyntheticResolutionLog, User


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
        fixture_group = Group.objects.create(name="fixture-group")
        exclusive_pre_access = Permission.objects.get(codename="exclusive_pre_access")
        requester.groups.add(fixture_group)
        requester.user_permissions.add(exclusive_pre_access)
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
        resolution_log = SyntheticResolutionLog.objects.create(
            entity_type=SyntheticResolutionLog.EntityType.DEBATER,
            synthetic_id=9001,
            synthetic_name="Synthetic Casey",
            resolved_to_id=debater.pk,
            resolved_to_name=debater.name,
            actor=reviewer,
            reason="Fixture coverage",
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
        first_group_index = next(
            index
            for index, item in enumerate(payload)
            if item["model"] == "auth.group"
        )
        first_permission_index = next(
            index
            for index, item in enumerate(payload)
            if item["model"] == "auth.permission"
        )
        user_items = [item for item in payload if item["model"] == "core.user"]
        requester_user_item = next(
            item
            for item in payload
            if item["model"] == "core.user" and item["fields"]["username"] == f"user_{requester.id}"
        )
        resolution_log_item = next(
            item
            for item in payload
            if item["model"] == "core.syntheticresolutionlog" and item["pk"] == resolution_log.pk
        )

        self.assertEqual(
            claim_request_item["fields"]["requested_by"],
            [f"user_{requester.id}"],
        )
        self.assertEqual(
            claim_request_item["fields"]["processed_by"],
            [f"user_{reviewer.id}"],
        )
        self.assertEqual(debater_item["fields"]["user"], [f"user_{requester.id}"])
        self.assertEqual(
            requester_user_item["fields"]["groups"],
            [["fixture-group"]],
        )
        self.assertEqual(
            requester_user_item["fields"]["user_permissions"],
            [["exclusive_pre_access", "core", "user"]],
        )
        self.assertEqual(
            resolution_log_item["fields"]["actor"],
            [f"user_{reviewer.id}"],
        )
        self.assertLess(first_group_index, first_user_index)
        self.assertLess(first_permission_index, first_user_index)
        self.assertLess(first_user_index, claim_request_index)
        self.assertCountEqual(
            [item["fields"]["username"] for item in user_items],
            [f"user_{requester.id}", f"user_{reviewer.id}"],
        )
