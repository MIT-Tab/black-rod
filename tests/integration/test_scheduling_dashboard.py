import json

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from core.models import SchedulerWorkspace, SchedulingRun


SCHOOLS_CSV = """\
,,,,,9/19-9/20,9/26-9/27
,,,,# of Tournaments,1,1
School Name,Region,Desired Tournaments,Priority,Tags,,
Alpha,North,1,1,,Rank 1,Rank 2
Beta,South,1,1,Unopposed,Rank 2,Rank 1
"""

DATES_CSV = """\
,# of Tournaments,Tags
9/19-9/20,1,
9/26-9/27,1,Unopposed
"""


class SchedulingDashboardViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.superuser = get_user_model().objects.create_superuser(
            username="scheduler-admin",
            email="scheduler-admin@example.com",
            password="password",
        )
        self.user = get_user_model().objects.create_user(
            username="regular-user",
            email="regular@example.com",
            password="password",
        )

    def test_superuser_can_access_dashboard(self):
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("core:scheduling_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Scheduling Workspace")
        self.assertContains(response, "Config and Instructions")
        self.assertContains(response, "Scheduling Runs")
        self.assertContains(response, "Run Scheduler In Browser")
        self.assertContains(response, "Central school on 2-tournament weekend")
        self.assertContains(response, reverse("core:scheduling_workspace_data"))
        self.assertContains(response, reverse("core:scheduling_save_browser_run"))

    def test_non_superuser_cannot_access_dashboard(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("core:scheduling_dashboard"))

        self.assertEqual(response.status_code, 403)

    def test_upload_csvs_saves_workspace(self):
        self.client.force_login(self.superuser)
        workspace = SchedulerWorkspace.get_solo()

        response = self.client.post(
            reverse("core:scheduling_dashboard"),
            {
                "action": "upload_csv",
                "workspace_version": workspace.version,
                "schools_csv": SimpleUploadedFile(
                    "schools.csv",
                    SCHOOLS_CSV.encode("utf-8"),
                    content_type="text/csv",
                ),
                "dates_csv": SimpleUploadedFile(
                    "dates.csv",
                    DATES_CSV.encode("utf-8"),
                    content_type="text/csv",
                ),
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        workspace.refresh_from_db()
        self.assertEqual(workspace.schools_filename, "schools.csv")
        self.assertEqual(workspace.dates_filename, "dates.csv")
        self.assertIn("Alpha", workspace.schools_csv)
        self.assertIn("9/19-9/20", workspace.dates_csv)
        messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertTrue(any("Scheduler CSVs saved." in message for message in messages))

    def test_stale_save_is_rejected(self):
        self.client.force_login(self.superuser)
        workspace = SchedulerWorkspace.get_solo()
        workspace.version = 3
        workspace.save(update_fields=["version"])

        response = self.client.post(
            reverse("core:scheduling_dashboard"),
            {
                "action": "save_settings",
                "workspace_version": 2,
                "max_workers": 2,
                "already_scheduled_penalty": 1,
                "rank_1_penalty": 2,
                "rank_2_penalty": 3,
                "rank_3_penalty": 4,
                "impossible_penalty": 5,
                "missing_unopposed_host_penalty": 6,
                "missing_requested_unopposed_penalty": 7,
                "missing_tag_penalty": 8,
                "tag_bonus": 9,
                "north_to_south_penalty": 10,
                "north_to_central_penalty": 11,
                "south_to_central_penalty": 12,
                "central_to_north_penalty": 13,
                "central_to_south_penalty": 14,
                "central_on_two_tournament_weekend_penalty": 15,
                "south_to_north_penalty": 16,
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        workspace.refresh_from_db()
        self.assertEqual(workspace.version, 3)
        self.assertEqual(workspace.settings, {})
        messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertTrue(any("This scheduler page changed in the meantime." in message for message in messages))

    def test_workspace_data_endpoint_returns_saved_snapshot(self):
        self.client.force_login(self.superuser)
        workspace = SchedulerWorkspace.get_solo()
        workspace.schools_csv = SCHOOLS_CSV
        workspace.dates_csv = DATES_CSV
        workspace.schools_filename = "schools.csv"
        workspace.dates_filename = "dates.csv"
        workspace.settings = {"max_workers": 1}
        workspace.save()

        response = self.client.get(reverse("core:scheduling_workspace_data"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["workspace"]["version"], workspace.version)
        self.assertEqual(payload["workspace"]["summary"]["school_count"], 2)
        self.assertEqual(payload["workspace"]["summary"]["scenario_count"], 1)
        self.assertIn("Alpha", payload["workspace"]["schools_csv"])

    def test_save_browser_run_creates_history_entry(self):
        self.client.force_login(self.superuser)
        workspace = SchedulerWorkspace.get_solo()
        workspace.schools_csv = SCHOOLS_CSV
        workspace.dates_csv = DATES_CSV
        workspace.schools_filename = "schools.csv"
        workspace.dates_filename = "dates.csv"
        workspace.settings = {"max_workers": 1}
        workspace.save()

        response = self.client.post(
            reverse("core:scheduling_save_browser_run"),
            data=json.dumps(
                {
                    "status": "completed",
                    "workspace_version": workspace.version,
                    "settings_snapshot": {"max_workers": 1},
                    "output_text": "Total Penalty: 123 Seed: 7",
                    "result": {
                        "best_seed": 7,
                        "best_penalty": 123,
                        "unmatched_schools": [],
                        "schedule": [
                            {
                                "date": "9/19-9/20",
                                "weekend_count": 1,
                                "tags": [],
                                "assignments": [],
                            }
                        ],
                        "output_text": "Total Penalty: 123 Seed: 7",
                        "summary": {
                            "school_count": 2,
                            "date_count": 2,
                            "scheduled_dates": 1,
                            "unmatched_school_count": 0,
                            "flexible_date_count": 0,
                            "scenario_count": 1,
                        },
                    },
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])

        run = SchedulingRun.objects.get()
        self.assertEqual(run.status, SchedulingRun.STATUS_COMPLETED)
        self.assertEqual(run.created_by, self.superuser)
        self.assertEqual(run.result["best_seed"], 7)
