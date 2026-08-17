from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import Motion, MotionTopic, MotionUserStatus
from core.utils.motion_import import read_motion_spreadsheet


class MotionViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("motion-user", password="test")
        self.done = Motion.objects.create(text="This house would test completed motions")
        self.available = Motion.objects.create(text="This house would test available motions")
        MotionUserStatus.objects.create(user=self.user, motion=self.done, status="done")

    def test_default_filter_excludes_current_users_done_and_ignored(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:motion_list"))
        self.assertContains(response, self.available.text)
        self.assertNotContains(response, self.done.text)

    def test_status_is_per_user(self):
        other = get_user_model().objects.create_user("other-motion-user")
        MotionUserStatus.objects.create(user=other, motion=self.available, status="ignore")
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:motion_list"))
        self.assertContains(response, self.available.text)

    def test_status_endpoint_sets_and_clears_status(self):
        self.client.force_login(self.user)
        url = reverse("core:motion_status", kwargs={"pk": self.available.pk})
        self.client.post(url, {"status": "ignore"})
        self.assertTrue(MotionUserStatus.objects.filter(user=self.user, motion=self.available, status="ignore").exists())
        self.client.post(url, {"status": "clear"})
        self.assertFalse(MotionUserStatus.objects.filter(user=self.user, motion=self.available).exists())

    def test_best_match_ranks_more_overlapping_topics_first(self):
        self.available.tags.add("economics", "environment")
        partial = Motion.objects.create(text="Partial match")
        partial.tags.add("economics")
        response = self.client.get(reverse("core:motion_list"), {"topics": "economics,environment", "progress": "all"})
        motions = list(response.context["filter"].qs)
        self.assertEqual(motions[:2], [self.available, partial])

    def test_motion_topics_use_their_own_tag_model(self):
        self.available.tags.add("motion-only-topic")
        self.assertTrue(MotionTopic.objects.filter(name="motion-only-topic").exists())
        tag_model = self.available.tags.through._meta.get_field("tag").remote_field.model
        self.assertEqual(tag_model, MotionTopic)

    def test_date_set_range(self):
        self.available.date_set = "2026-02-01"
        self.available.save(update_fields=["date_set"])
        response = self.client.get(
            reverse("core:motion_list"),
            {
                "date_from": "2026-01-01",
                "date_to": "2026-03-01",
                "progress": "all",
            },
        )
        self.assertContains(response, self.available.text)
        self.assertNotContains(response, self.done.text)

    def test_progress_filter_has_a_valid_label(self):
        response = self.client.get(reverse("core:motion_list"))
        self.assertContains(response, "Progress")
        self.assertNotContains(response, "[invalid name]")

    def test_filter_panel_has_clear_link(self):
        response = self.client.get(
            reverse("core:motion_list"), {"text__icontains": "test"}
        )
        self.assertContains(
            response,
            f'<a class="btn btn-outline-secondary ml-2" href="{reverse("core:motion_list")}">Clear</a>',
            html=True,
        )

    def test_list_status_and_topics_do_not_cause_n_plus_one_queries(self):
        self.client.force_login(self.user)
        self.available.tags.add("economics", "law")
        with CaptureQueriesContext(connection) as small_context:
            self.client.get(reverse("core:motion_list"))

        for index in range(25):
            motion = Motion.objects.create(text=f"N+1 test motion {index}")
            motion.tags.add("economics", f"topic-{index % 3}")
            if index % 4 == 0:
                MotionUserStatus.objects.create(
                    user=self.user, motion=motion, status=MotionUserStatus.DONE
                )

        with CaptureQueriesContext(connection) as large_context:
            self.client.get(reverse("core:motion_list"))

        self.assertLessEqual(len(large_context), len(small_context) + 1)
        status_queries = [
            query["sql"]
            for query in large_context.captured_queries
            if "core_motionuserstatus" in query["sql"].lower()
        ]
        self.assertLessEqual(len(status_queries), 2)


class MotionImportTests(TestCase):
    def test_reads_csv_and_marks_duplicates(self):
        Motion.objects.create(text="Existing motion")
        upload = SimpleUploadedFile(
            "motions.csv",
            b"motion_text,background_slide,date_set,tags\nExisting motion,,2026-01-02,law\nNew motion,Context,01/03/2026,law; rights\n",
            content_type="text/csv",
        )
        rows = read_motion_spreadsheet(upload)
        self.assertTrue(rows[0]["duplicate"])
        self.assertEqual(rows[1]["tags"], ["law", "rights"])
        self.assertEqual(rows[1]["date_set"], "2026-01-03")
