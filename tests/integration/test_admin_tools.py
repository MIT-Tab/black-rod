from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse
from unittest.mock import patch

from core.models import Debater, School, SyntheticResolutionLog, Team
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

    def test_admin_tools_shows_synthetic_resolution_form(self):
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("core:admin_tools"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalidate ELO Cache")
        self.assertContains(response, reverse("core:invalidate_elo_cache"))
        self.assertContains(response, "Resolve Synthetic Entity")
        self.assertContains(response, reverse("core:synthetic_resolution"))

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
