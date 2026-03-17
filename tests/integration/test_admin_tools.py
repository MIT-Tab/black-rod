from datetime import date
import json
import os
import tempfile

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from unittest.mock import patch

from core.models import Debater, Round, School, SyntheticResolutionLog, Team, Tournament
from core.views.admin_views import MergeSuggestionsView
from core.views.elo_cache import get_cached_elo_state, set_cached_elo_state


class AdminToolsViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.superuser = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="password",
        )
        self.school = School.objects.create(name="Resolver University")
        cache.delete("merge_suggestions_list")
        cache.delete("synthetic_resolution_suggestions_list")

    def test_admin_tools_shows_synthetic_resolution_form(self):
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("core:admin_tools"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalidate ELO Cache")
        self.assertContains(response, reverse("core:invalidate_elo_cache"))
        self.assertContains(response, "Resolve Synthetic Entity")
        self.assertContains(response, reverse("core:synthetic_resolution"))
        self.assertContains(response, reverse("core:synthetic_resolution_suggestions"))
        self.assertContains(response, reverse("core:scheduling_dashboard"))
        self.assertContains(response, reverse("core:school_short_name_audit"))
        self.assertContains(response, "Scheduling Workspace")
        self.assertContains(response, "School Short Names")
        self.assertContains(response, 'id="synthetic_search"', html=False)
        self.assertContains(response, 'id="target_search"', html=False)
        self.assertContains(response, reverse("core:round_amendments_upload"))
        self.assertContains(response, "Round Amendments")

    def test_round_amendment_upload_creates_round(self):
        self.client.force_login(self.superuser)
        tournament = Tournament.objects.create(
            host=self.school,
            date=date(2024, 2, 10),
            season="2024",
            manual_name="Admin Upload Open",
            num_rounds=5,
        )
        gov_one = Debater.objects.create(first_name="Gov", last_name="One", school=self.school)
        gov_two = Debater.objects.create(first_name="Gov", last_name="Two", school=self.school)
        opp_one = Debater.objects.create(first_name="Opp", last_name="One", school=self.school)
        opp_two = Debater.objects.create(first_name="Opp", last_name="Two", school=self.school)

        payload = b"""{
  "rounds": {
    "create": [
      {
        "tournament_id": %d,
        "gov_debater_ids": [%d, %d],
        "opp_debater_ids": [%d, %d],
        "round_number": 1,
        "stage": "prelim",
        "round_label": "Upload Round",
        "victor": 1,
        "import_key": "upload-round-1",
        "import_origin": "file_backup"
      }
    ]
  }
}""" % (
            tournament.id,
            gov_one.id,
            gov_two.id,
            opp_one.id,
            opp_two.id,
        )

        response = self.client.post(
            reverse("core:round_amendments_upload"),
            {
                "amendment_file": SimpleUploadedFile(
                    "round-amendments.json",
                    payload,
                    content_type="application/json",
                )
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Round.objects.filter(
                tournament=tournament,
                import_key="upload-round-1",
                round_label="Upload Round",
            ).exists()
        )
        messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertTrue(any(message.startswith("Applied 1 amendment actions.") for message in messages))

    def test_merge_suggestions_builder_excludes_synthetic_debaters(self):
        first = Debater.objects.create(
            first_name="Merge",
            last_name="Candidate",
            school=self.school,
        )
        second = Debater.objects.create(
            first_name="Merge",
            last_name="Candidate",
            school=self.school,
        )
        synthetic = Debater.all_objects.create(
            first_name="Merge",
            last_name="Candidate",
            school=self.school,
            temporary=True,
            synthetic=True,
        )

        suggestions = MergeSuggestionsView()._build_suggestions()

        self.assertTrue(suggestions)
        suggestion_ids = {
            suggestion["debater_one"].id
            for suggestion in suggestions
        } | {
            suggestion["debater_two"].id
            for suggestion in suggestions
        }
        self.assertIn(first.id, suggestion_ids)
        self.assertIn(second.id, suggestion_ids)
        self.assertNotIn(synthetic.id, suggestion_ids)

    def test_synthetic_resolution_suggestions_page_lists_synthetic_and_canonical_pair(self):
        self.client.force_login(self.superuser)
        synthetic = Debater.all_objects.create(
            first_name="Casey",
            last_name="Browne",
            school=self.school,
            temporary=True,
            synthetic=True,
        )
        canonical = Debater.objects.create(
            first_name="Casey",
            last_name="Brown",
            school=self.school,
        )

        response = self.client.get(
            reverse("core:synthetic_resolution_suggestions"),
            {"refresh": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synthetic Debater Resolution Suggestions")
        self.assertContains(response, synthetic.name)
        self.assertContains(response, canonical.name)

    def test_synthetic_resolution_suggestions_post_resolves_debater(self):
        self.client.force_login(self.superuser)
        synthetic = Debater.all_objects.create(
            first_name="Syn",
            last_name="Thetic",
            school=self.school,
            temporary=True,
            synthetic=True,
        )
        canonical = Debater.objects.create(
            first_name="Canonical",
            last_name="Debater",
            school=self.school,
        )

        response = self.client.post(
            reverse("core:synthetic_resolution_suggestions"),
            {
                "synthetic_debater": str(synthetic.id),
                "canonical_debater": str(canonical.id),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {
                "success": True,
                "message": f"Resolved synthetic debater {synthetic.name} into {canonical.name}.",
            },
        )
        self.assertFalse(Debater.all_objects.filter(pk=synthetic.pk).exists())
        self.assertTrue(Debater.all_objects.filter(pk=canonical.pk).exists())
        self.assertTrue(
            SyntheticResolutionLog.objects.filter(
                entity_type=SyntheticResolutionLog.EntityType.DEBATER,
                synthetic_id=synthetic.id,
                resolved_to_id=canonical.id,
            ).exists()
        )

    def test_elo_cache_invalidation_clears_cached_dashboard_state(self):
        self.client.force_login(self.superuser)
        cached_request = type(
            "CacheRequest",
            (),
            {
                "user": type(
                    "CacheUser",
                    (),
                    {
                        "is_authenticated": True,
                        "pk": self.superuser.pk,
                    },
                )(),
                "session": None,
            },
        )()
        set_cached_elo_state(cached_request, {"ranking_rows": []})
        self.assertEqual(get_cached_elo_state(cached_request), {"ranking_rows": []})

        with patch("core.views.admin_views.clear_runtime_caches") as clear_runtime_caches:
            response = self.client.post(
                reverse("core:invalidate_elo_cache"),
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(get_cached_elo_state(cached_request))
        clear_runtime_caches.assert_called_once_with()

        messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertTrue(
            any(message.startswith("ELO cache invalidated successfully.") for message in messages)
        )

    def test_synthetic_debater_resolution_restores_resolver_flow(self):
        self.client.force_login(self.superuser)
        target = Debater.objects.create(
            first_name="Target",
            last_name="Debater",
            school=self.school,
        )
        synthetic = Debater.all_objects.create(
            first_name="Synthetic",
            last_name="Debater",
            school=self.school,
            temporary=True,
            synthetic=True,
        )
        unaffiliated_partner = Debater.objects.create(
            first_name="Unaffiliated",
            last_name="Partner",
            school=None,
        )
        synthetic_team = Team.objects.create(name="Synthetic Team")
        synthetic_team.debaters.set([synthetic, unaffiliated_partner])

        response = self.client.post(
            reverse("core:synthetic_resolution"),
            {
                "entity_type": "debater",
                "synthetic_id": str(synthetic.id),
                "target_id": str(target.id),
                "reason": "manual reconciliation",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Debater.all_objects.filter(pk=synthetic.pk).exists())
        self.assertTrue(Debater.all_objects.filter(pk=target.pk).exists())
        synthetic_team.refresh_from_db()
        self.assertEqual(
            set(synthetic_team.debaters.values_list("id", flat=True)),
            {target.id, unaffiliated_partner.id},
        )

        log = SyntheticResolutionLog.objects.get(
            entity_type=SyntheticResolutionLog.EntityType.DEBATER,
            synthetic_id=synthetic.id,
            resolved_to_id=target.id,
        )
        self.assertEqual(log.reason, "manual reconciliation")

        messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertIn("Synthetic entity resolved successfully.", messages)

    def test_synthetic_resolution_records_round_amendment_in_development(self):
        self.client.force_login(self.superuser)
        target = Debater.objects.create(
            first_name="Target",
            last_name="Debater",
            school=self.school,
        )
        synthetic = Debater.all_objects.create(
            first_name="Synthetic",
            last_name="Debater",
            school=self.school,
            temporary=True,
            synthetic=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            amendment_path = os.path.join(temp_dir, "round-amendments.local.json")
            with override_settings(ENV="development", ROUND_AMENDMENTS_FILE=amendment_path):
                response = self.client.post(
                    reverse("core:synthetic_resolution"),
                    {
                        "entity_type": "debater",
                        "synthetic_id": str(synthetic.id),
                        "target_id": str(target.id),
                        "reason": "record this change",
                    },
                    follow=True,
                )

            self.assertEqual(response.status_code, 200)
            with open(amendment_path, "r", encoding="utf-8") as handle:
                recorded = json.load(handle)
            self.assertEqual(len(recorded["actions"]), 1)
            self.assertEqual(recorded["actions"][0]["type"], "resolve_synthetic")
            self.assertEqual(recorded["actions"][0]["synthetic_id"], synthetic.id)
            self.assertEqual(recorded["actions"][0]["target_id"], target.id)

    def test_resolver_debater_autocomplete_supports_id_and_synthetic_filter(self):
        self.client.force_login(self.superuser)
        synthetic = Debater.all_objects.create(
            first_name="Search",
            last_name="Synthetic",
            school=self.school,
            temporary=True,
            synthetic=True,
        )
        canonical = Debater.objects.create(
            first_name="Search",
            last_name="Canonical",
            school=self.school,
        )

        synthetic_response = self.client.get(
            reverse("core:debater_autocomplete"),
            {"q": str(synthetic.id), "synthetic": "true"},
        )
        self.assertEqual(synthetic_response.status_code, 200)
        self.assertContains(synthetic_response, synthetic.name)
        self.assertNotContains(synthetic_response, canonical.name)

        canonical_response = self.client.get(
            reverse("core:debater_autocomplete"),
            {"q": self.school.name, "synthetic": "false"},
        )
        self.assertEqual(canonical_response.status_code, 200)
        self.assertContains(canonical_response, canonical.name)
        self.assertNotContains(canonical_response, synthetic.name)

    def test_resolver_school_autocomplete_supports_id_and_synthetic_filter(self):
        self.client.force_login(self.superuser)
        synthetic_school = School.objects.create(name="Resolver Synthetic College", synthetic=True)
        canonical_school = School.objects.create(name="Resolver Canonical College", synthetic=False)

        synthetic_response = self.client.get(
            reverse("core:school_autocomplete"),
            {"q": "Resolver", "synthetic": "true"},
        )
        self.assertEqual(synthetic_response.status_code, 200)
        self.assertContains(synthetic_response, synthetic_school.name)
        self.assertNotContains(synthetic_response, canonical_school.name)

        canonical_response = self.client.get(
            reverse("core:school_autocomplete"),
            {"q": str(canonical_school.id), "synthetic": "false"},
        )
        self.assertEqual(canonical_response.status_code, 200)
        self.assertContains(canonical_response, canonical_school.name)
        self.assertNotContains(canonical_response, synthetic_school.name)

    def test_resolver_team_autocomplete_supports_id_and_synthetic_filter(self):
        self.client.force_login(self.superuser)
        school = School.objects.create(name="Resolver Team School")
        synthetic_member = Debater.objects.create(
            first_name="Synthetic",
            last_name="Partner",
            school=school,
        )
        canonical_member = Debater.objects.create(
            first_name="Canonical",
            last_name="Partner",
            school=school,
        )
        synthetic_team = Team.objects.create(name="Resolver Synthetic Team", synthetic=True)
        synthetic_team.debaters.add(synthetic_member)
        canonical_team = Team.objects.create(name="Resolver Canonical Team", synthetic=False)
        canonical_team.debaters.add(canonical_member)

        synthetic_response = self.client.get(
            reverse("core:team_autocomplete"),
            {"q": school.name, "synthetic": "true"},
        )
        self.assertEqual(synthetic_response.status_code, 200)
        self.assertContains(synthetic_response, synthetic_team.name)
        self.assertNotContains(synthetic_response, canonical_team.name)

        canonical_response = self.client.get(
            reverse("core:team_autocomplete"),
            {"q": str(canonical_team.id), "synthetic": "false"},
        )
        self.assertEqual(canonical_response.status_code, 200)
        self.assertContains(canonical_response, canonical_team.name)
        self.assertNotContains(canonical_response, synthetic_team.name)
