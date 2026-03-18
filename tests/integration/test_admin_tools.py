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

from core.models import Debater, Round, RoundStats, School, SchoolLookup, SyntheticResolutionLog, Team, Tournament
from core.utils.round_amendment_recorder import (
    backfill_synthetic_resolution_actions_from_logs,
)
from core.views.admin_views import MergeSuggestionsView, SyntheticResolutionSuggestionsView
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
        cache.clear()

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
        self.assertContains(response, reverse("core:synthetic_cleanup"))
        self.assertContains(response, "Synthetic Cleanup")

    def test_synthetic_cleanup_lists_direct_and_isolated_synthetic_entities(self):
        self.client.force_login(self.superuser)

        unused_school = School.all_objects.create(
            name="Unused Synthetic School",
            short_name="USS",
            synthetic=True,
        )
        used_school = School.all_objects.create(
            name="Used Synthetic School",
            short_name="USS2",
            synthetic=True,
        )
        SchoolLookup.objects.create(
            server_name="used-synthetic-school",
            school=used_school,
        )

        unused_debater = Debater.all_objects.create(
            first_name="Unused",
            last_name="Synthetic",
            school=self.school,
            synthetic=True,
        )
        used_debater = Debater.all_objects.create(
            first_name="Used",
            last_name="Synthetic",
            school=self.school,
            synthetic=True,
        )
        linked_team = Team.objects.create(name="Linked Team", short_name="LT", synthetic=True)
        linked_team.debaters.add(used_debater)

        unused_team = Team.objects.create(name="Unused Synthetic Team", short_name="UST", synthetic=True)
        used_team = Team.objects.create(name="Used Synthetic Team", short_name="UST2", synthetic=True)
        used_team.debaters.add(
            Debater.all_objects.create(
                first_name="Synthetic",
                last_name="Member",
                school=self.school,
                synthetic=True,
            )
        )

        isolated_school = School.all_objects.create(
            name="Isolated Synthetic School",
            short_name="ISS",
            synthetic=True,
        )
        isolated_first = Debater.all_objects.create(
            first_name="Isolated",
            last_name="First",
            school=isolated_school,
            synthetic=True,
        )
        isolated_second = Debater.all_objects.create(
            first_name="Isolated",
            last_name="Second",
            school=isolated_school,
            synthetic=True,
        )
        isolated_team = Team.objects.create(
            name="Isolated Synthetic Team",
            short_name="IST",
            synthetic=True,
        )
        isolated_team.debaters.add(isolated_first, isolated_second)

        externally_linked_school = School.all_objects.create(
            name="Externally Linked Synthetic School",
            short_name="ELS",
            synthetic=True,
        )
        externally_linked_debater = Debater.all_objects.create(
            first_name="Externally",
            last_name="Linked",
            school=externally_linked_school,
            synthetic=True,
        )
        externally_linked_team = Team.objects.create(
            name="Externally Linked Synthetic Team",
            short_name="ELT",
            synthetic=True,
        )
        externally_linked_team.debaters.add(externally_linked_debater)
        Debater.objects.create(
            first_name="Real",
            last_name="Link",
            school=externally_linked_school,
        )

        response = self.client.get(reverse("core:synthetic_cleanup"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, unused_school.name)
        self.assertContains(response, unused_debater.name)
        self.assertContains(response, unused_team.name)
        self.assertContains(response, isolated_school.name)
        self.assertContains(response, isolated_first.name)
        self.assertContains(response, isolated_second.name)
        self.assertContains(response, isolated_team.name)
        self.assertNotContains(response, used_school.name)
        self.assertNotContains(response, used_debater.name)
        self.assertNotContains(response, used_team.name)
        self.assertNotContains(response, externally_linked_school.name)
        self.assertNotContains(response, externally_linked_debater.name)
        self.assertNotContains(response, externally_linked_team.name)

    def test_synthetic_cleanup_deletes_selected_entities_and_rechecks_references(self):
        self.client.force_login(self.superuser)

        unused_school = School.all_objects.create(
            name="Delete Synthetic School",
            short_name="DSS",
            synthetic=True,
        )
        unused_debater = Debater.all_objects.create(
            first_name="Delete",
            last_name="Synthetic",
            school=self.school,
            synthetic=True,
        )
        unused_team = Team.objects.create(name="Delete Synthetic Team", short_name="DST", synthetic=True)

        isolated_school = School.all_objects.create(
            name="Delete Isolated Synthetic School",
            short_name="DISS",
            synthetic=True,
        )
        isolated_first = Debater.all_objects.create(
            first_name="Delete",
            last_name="Isolated One",
            school=isolated_school,
            synthetic=True,
        )
        isolated_second = Debater.all_objects.create(
            first_name="Delete",
            last_name="Isolated Two",
            school=isolated_school,
            synthetic=True,
        )
        isolated_team = Team.objects.create(name="Delete Isolated Synthetic Team", short_name="DIST", synthetic=True)
        isolated_team.debaters.add(isolated_first, isolated_second)

        partial_school = School.all_objects.create(
            name="Partial Synthetic School",
            short_name="PSS",
            synthetic=True,
        )
        partial_debater = Debater.all_objects.create(
            first_name="Partial",
            last_name="Synthetic",
            school=partial_school,
            synthetic=True,
        )
        partial_team = Team.objects.create(name="Partial Synthetic Team", short_name="PST", synthetic=True)
        partial_team.debaters.add(partial_debater)

        response = self.client.post(
            reverse("core:synthetic_cleanup"),
            {
                "selected_ids": [
                    f"school:{unused_school.id}",
                    f"debater:{unused_debater.id}",
                    f"team:{unused_team.id}",
                    f"school:{isolated_school.id}",
                    f"debater:{isolated_first.id}",
                    f"debater:{isolated_second.id}",
                    f"team:{isolated_team.id}",
                    f"school:{partial_school.id}",
                    f"team:{partial_team.id}",
                ]
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(School.all_objects.filter(pk=unused_school.pk).exists())
        self.assertFalse(Debater.all_objects.filter(pk=unused_debater.pk).exists())
        self.assertFalse(Team.objects.filter(pk=unused_team.pk).exists())
        self.assertFalse(School.all_objects.filter(pk=isolated_school.pk).exists())
        self.assertFalse(Debater.all_objects.filter(pk=isolated_first.pk).exists())
        self.assertFalse(Debater.all_objects.filter(pk=isolated_second.pk).exists())
        self.assertFalse(Team.objects.filter(pk=isolated_team.pk).exists())
        self.assertTrue(School.all_objects.filter(pk=partial_school.pk).exists())
        self.assertTrue(Team.objects.filter(pk=partial_team.pk).exists())

        messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertIn("Deleted 7 synthetic cleanup entities.", messages)
        self.assertTrue(any(message.startswith("Skipped 2 selections") for message in messages))

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

    def test_synthetic_resolution_suggestions_page_waits_for_run(self):
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
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Synthetic Resolution Suggestions")
        self.assertContains(response, "No suggestions have been loaded yet")
        self.assertNotContains(response, synthetic.name)
        self.assertNotContains(response, canonical.name)

    def test_synthetic_resolution_suggestions_page_lists_scoped_debater_pair(self):
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
            {
                "run": "1",
                "synthetic_debaters": [str(synthetic.id)],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Debater Suggestions")
        self.assertContains(response, synthetic.name)
        self.assertContains(response, canonical.name)

    def test_synthetic_resolution_suggestions_page_lists_scoped_school_pair(self):
        self.client.force_login(self.superuser)
        synthetic_school = School.all_objects.create(
            name="Resolver Univ",
            short_name="RU",
            temporary=True,
            synthetic=True,
        )
        canonical_school = School.objects.create(
            name="Resolver University Main",
            short_name="RU",
        )

        response = self.client.get(
            reverse("core:synthetic_resolution_suggestions"),
            {
                "run": "1",
                "synthetic_schools": [str(synthetic_school.id)],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "School Suggestions")
        self.assertContains(response, synthetic_school.name)
        self.assertContains(response, canonical_school.name)

    def test_synthetic_resolution_suggestions_school_scope_uses_name_blocking_beyond_recent_slice(self):
        self.client.force_login(self.superuser)
        synthetic_school = School.all_objects.create(
            name="Synthetic Resolver School",
            short_name="SRS",
            temporary=True,
            synthetic=True,
        )
        synthetic = Debater.all_objects.create(
            first_name="Casey",
            last_name="Browne",
            school=synthetic_school,
            temporary=True,
            synthetic=True,
        )
        canonical = Debater.objects.create(
            first_name="Casey",
            last_name="Brown",
            school=self.school,
        )
        for index in range(10):
            Debater.objects.create(
                first_name=f"Recent{index}",
                last_name=f"Filler{index}",
                school=self.school,
            )

        with patch.object(SyntheticResolutionSuggestionsView, "default_canonical_debater_limit", 5):
            response = self.client.get(
                reverse("core:synthetic_resolution_suggestions"),
                {
                    "run": "1",
                    "synthetic_schools": [str(synthetic_school.id)],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, synthetic.name)
        self.assertContains(response, canonical.name)

    def test_synthetic_resolution_suggestions_page_matches_school_acronyms(self):
        self.client.force_login(self.superuser)
        synthetic_school = School.all_objects.create(
            name="UCLA",
            short_name="",
            temporary=True,
            synthetic=True,
        )
        canonical_school = School.objects.create(
            name="University of California Los Angeles",
            short_name="",
        )

        response = self.client.get(
            reverse("core:synthetic_resolution_suggestions"),
            {
                "run": "1",
                "synthetic_schools": [str(synthetic_school.id)],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, synthetic_school.name)
        self.assertContains(response, canonical_school.name)

    def test_synthetic_resolution_suggestions_post_rejects_and_filters_pair(self):
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

        response = self.client.post(
            reverse("core:synthetic_resolution_suggestions"),
            {
                "action": "reject",
                "entity_type": "debater",
                "synthetic_id": str(synthetic.id),
                "target_id": str(canonical.id),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {
                "success": True,
                "message": f"Rejected synthetic debater suggestion {synthetic.name} -> {canonical.name}.",
                "action": "reject",
            },
        )
        self.assertTrue(Debater.all_objects.filter(pk=synthetic.pk).exists())
        self.assertTrue(
            SyntheticResolutionLog.objects.filter(
                action=SyntheticResolutionLog.Action.REJECTED,
                entity_type=SyntheticResolutionLog.EntityType.DEBATER,
                synthetic_id=synthetic.id,
                resolved_to_id=canonical.id,
            ).exists()
        )

        follow_up = self.client.get(
            reverse("core:synthetic_resolution_suggestions"),
            {
                "run": "1",
                "synthetic_debaters": [str(synthetic.id)],
                "refresh": "1",
            },
        )

        self.assertEqual(follow_up.status_code, 200)
        self.assertContains(follow_up, "No synthetic resolution candidates found")
        self.assertEqual(follow_up.context["debater_suggestions"], [])

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
                "action": "resolve",
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

    @override_settings(ENV="development")
    def test_synthetic_resolution_suggestions_post_records_round_amendment(self):
        self.client.force_login(self.superuser)
        synthetic = Debater.all_objects.create(
            first_name="Log",
            last_name="Me",
            school=self.school,
            temporary=True,
            synthetic=True,
        )
        canonical = Debater.objects.create(
            first_name="Keep",
            last_name="Me",
            school=self.school,
        )

        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as handle:
            json.dump({"actions": []}, handle)
            temp_path = handle.name

        try:
            with override_settings(ROUND_AMENDMENTS_FILE=temp_path):
                response = self.client.post(
                    reverse("core:synthetic_resolution_suggestions"),
                    {
                        "synthetic_debater": str(synthetic.id),
                        "canonical_debater": str(canonical.id),
                    },
                )

            self.assertEqual(response.status_code, 200)
            with open(temp_path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        finally:
            os.unlink(temp_path)

        self.assertEqual(len(document["actions"]), 1)
        self.assertEqual(
            document["actions"][0],
            {
                "type": "resolve_synthetic",
                "entity_type": "debater",
                "synthetic_id": synthetic.id,
                "target_id": canonical.id,
                "reason": "Suggested synthetic debater resolution",
            },
        )

    def test_synthetic_resolution_suggestions_post_resolves_school(self):
        self.client.force_login(self.superuser)
        synthetic_school = School.all_objects.create(
            name="Synthetic College",
            short_name="SC",
            temporary=True,
            synthetic=True,
        )
        canonical_school = School.objects.create(
            name="Synthetic College University",
            short_name="SC",
        )

        response = self.client.post(
            reverse("core:synthetic_resolution_suggestions"),
            {
                "entity_type": "school",
                "synthetic_id": str(synthetic_school.id),
                "target_id": str(canonical_school.id),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {
                "success": True,
                "message": f"Resolved synthetic school {synthetic_school.name} into {canonical_school.name}.",
                "action": "resolve",
            },
        )
        self.assertFalse(School.all_objects.filter(pk=synthetic_school.pk).exists())
        self.assertTrue(School.all_objects.filter(pk=canonical_school.pk).exists())
        self.assertTrue(
            SyntheticResolutionLog.objects.filter(
                entity_type=SyntheticResolutionLog.EntityType.SCHOOL,
                synthetic_id=synthetic_school.id,
                resolved_to_id=canonical_school.id,
            ).exists()
        )

    @override_settings(ENV="development")
    def test_backfill_synthetic_resolution_actions_from_logs_adds_missing_actions(self):
        SyntheticResolutionLog.objects.create(
            entity_type=SyntheticResolutionLog.EntityType.DEBATER,
            synthetic_id=501,
            synthetic_name="Synthetic Debater",
            resolved_to_id=77,
            resolved_to_name="Canonical Debater",
            actor=self.superuser,
            reason="Suggested synthetic debater resolution",
            source_context={"source": "synthetic_resolution_suggestions"},
        )
        SyntheticResolutionLog.objects.create(
            entity_type=SyntheticResolutionLog.EntityType.SCHOOL,
            synthetic_id=601,
            synthetic_name="Synthetic School",
            resolved_to_id=88,
            resolved_to_name="Canonical School",
            actor=self.superuser,
            reason="Suggested synthetic school resolution",
            source_context={"source": "synthetic_resolution_suggestions"},
        )

        existing_action = {
            "type": "resolve_synthetic",
            "entity_type": "debater",
            "synthetic_id": 501,
            "target_id": 77,
            "reason": "Suggested synthetic debater resolution",
        }

        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as handle:
            json.dump({"actions": [existing_action]}, handle)
            temp_path = handle.name

        try:
            with override_settings(ROUND_AMENDMENTS_FILE=temp_path):
                summary = backfill_synthetic_resolution_actions_from_logs()

            with open(temp_path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        finally:
            os.unlink(temp_path)

        self.assertEqual(summary["recorded"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(len(document["actions"]), 2)
        self.assertEqual(document["actions"][0], existing_action)
        self.assertEqual(
            document["actions"][1],
            {
                "type": "resolve_synthetic",
                "entity_type": "school",
                "synthetic_id": 601,
                "target_id": 88,
                "reason": "Suggested synthetic school resolution",
            },
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
