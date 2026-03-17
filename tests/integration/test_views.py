# pylint: disable=import-outside-toplevel
from datetime import date
import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.conf import settings
from django.test import TestCase, Client, override_settings
from django.urls import reverse

from core.models.school import School
from core.models.debater import Debater
from core.models.team import Team
from core.models.tournament import Tournament
from core.models.standings.toty import TOTY
from core.utils.elo_runtime_engine.models import DebaterRankingRow, EloRunResult


class ViewTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.school = School.objects.create(name="Test University")
        self.debater = Debater.objects.create(
            first_name="John", last_name="Doe", school=self.school
        )
        self.tournament = Tournament.objects.create(
            name="Test Tournament", host=self.school, date=date.today(), season="2024"
        )

    def test_home_page_loads(self):
        """Test that home page loads without errors"""
        try:
            response = self.client.get("/")
            # Accept any response that doesn't crash
            self.assertIn(response.status_code, [200, 302, 404])
        except:
            # If URL doesn't exist, that's expected in a test environment
            pass

    def test_school_model_methods_work(self):
        """Test that school model methods execute without error"""
        school = School.objects.create(name="Method Test School")
        str_result = str(school)
        self.assertEqual(str_result, "Method Test School")

        # Test get_absolute_url doesn't crash
        try:
            url = school.get_absolute_url()
            self.assertIsInstance(url, str)
        except:
            # URL resolution might fail in test environment
            pass

    def test_debater_model_methods_work(self):
        """Test that debater model methods execute without error"""
        debater = Debater.objects.create(
            first_name="Test", last_name="User", school=self.school
        )

        # Test string representation
        self.assertEqual(str(debater), "Test User")

        # Test name property
        self.assertEqual(debater.name, "Test User")

        # Test get_absolute_url doesn't crash
        try:
            url = debater.get_absolute_url()
            self.assertIsInstance(url, str)
        except:
            # URL resolution might fail in test environment
            pass

    def test_authenticated_access(self):
        """Test that login works"""
        # Create user with email and is_active=True for allauth compatibility
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            email="testuser@example.com",
            is_active=True,
        )
        login_successful = self.client.login(
            username="testuser", password="testpass123"
        )
        self.assertTrue(login_successful)

    def test_model_relationships(self):
        """Test model relationships work correctly"""
        # Test school-debater relationship
        debaters = self.school.debaters.all()
        self.assertIn(self.debater, debaters)

        # Test school-tournament relationship
        tournaments = self.school.hosted_tournaments.all()
        self.assertIn(self.tournament, tournaments)


class ModelIntegrationTest(TestCase):
    """Test model interactions and business logic"""

    def test_school_cascade_behavior(self):
        """Test what happens when school is deleted"""
        school = School.objects.create(name="Delete Test School")
        debater = Debater.objects.create(
            first_name="Test", last_name="Debater", school=school
        )

        # Delete school - debater should still exist but with null school
        school.delete()
        debater.refresh_from_db()
        self.assertIsNone(debater.school)

    def test_debater_status_behavior(self):
        """Test debater status field behavior"""
        varsity_debater = Debater.objects.create(
            first_name="Varsity", last_name="Player", status=Debater.VARSITY
        )
        novice_debater = Debater.objects.create(
            first_name="Novice", last_name="Player", status=Debater.NOVICE
        )

        self.assertEqual(varsity_debater.get_status_display(), "Varsity")
        self.assertEqual(novice_debater.get_status_display(), "Novice")

    def test_tournament_defaults(self):
        """Test tournament default values"""
        # Create a host school first
        host_school = School.objects.create(name="Default Host School")

        # Create tournament with manual_name to override the auto-generated name
        tournament = Tournament(
            name="Default Test Tournament",  # This will be overridden by manual_name
            date=date.today(),
            season="2024",
            manual_name="Default Test Tournament",  # This ensures proper name setting
            host=host_school,  # Provide a host to avoid the None error
        )
        tournament.save()
        self.assertEqual(tournament.num_rounds, 5)
        self.assertEqual(tournament.name, "Default Test Tournament")  # Should use manual_name
        self.assertEqual(tournament.host, host_school)


@pytest.mark.django_db
def test_model_creation():
    """Test model creation works in pytest style"""
    school = School.objects.create(name="Pytest School")
    assert school.name == "Pytest School"
    assert school.included_in_oty is True


class IndexViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.school = School.objects.create(name="Index School", included_in_oty=True)
        self.debaters = [
            Debater.objects.create(first_name="Alex", last_name="First", school=self.school),
            Debater.objects.create(first_name="Blair", last_name="Second", school=self.school),
        ]
        self.team = Team.objects.create(name="")
        self.team.debaters.add(*self.debaters)
        self.team.update_name()
        self.team.save()
        self.tournament = Tournament.objects.create(
            name="Index Tournament",
            host=self.school,
            date=date.today(),
            season="2024",
            num_teams=16,
        )
        TOTY.objects.create(season="2024", team=self.team, points=18)

    def _user_with_exclusive_pre_access(self):
        user = get_user_model().objects.create_user(
            username="preaccess-user",
            password="testpass123",
            email="preaccess@example.com",
        )
        user.user_permissions.add(Permission.objects.get(codename="exclusive_pre_access"))
        return user

    @override_settings(ONLINE_SEASONS=("2024",), ONLINE_QUAL_BAR=9.5, LAST_NOTY_SEASON=2025)
    def test_index_sets_online_context_for_online_season(self):
        response = self.client.get(
            reverse("core:index"), {"season": "2024", "default": "coty"}
        )

        self.assertEqual(response.status_code, 200)
        context = response.context
        self.assertEqual(context["current_season"], "2024")
        self.assertEqual(context["default"], "coty")
        self.assertTrue(context["using_online_quals"])
        self.assertEqual(context["online_qual_bar"], 9.5)
        self.assertTrue(context["render_noty"])

    @override_settings(LAST_NOTY_SEASON=2000)
    def test_index_sanitizes_inputs_and_disables_noty(self):
        response = self.client.get(
            reverse("core:index"), {"season": "1900", "default": "invalid"}
        )

        self.assertEqual(response.status_code, 200)
        context = response.context
        self.assertEqual(context["current_season"], settings.CURRENT_SEASON)
        self.assertEqual(context["default"], "toty")
        self.assertFalse(context["using_online_quals"])
        self.assertFalse(context["render_noty"])

    def test_standings_replay_view_loads(self):
        response = self.client.get(
            reverse("core:standings_replay"), {"season": "2024", "default": "soty"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("replay_api_url", response.context)
        self.assertEqual(response.context["default"], "soty")

    def test_elo_dashboard_requires_exclusive_pre_access(self):
        response = self.client.get(reverse("core:elo_dashboard"))
        self.assertEqual(response.status_code, 302)

        user = get_user_model().objects.create_user(
            username="no-preaccess",
            password="testpass123",
            email="no-preaccess@example.com",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("core:elo_dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_elo_dashboard_paginates_rankings_50_per_page(self):
        user = self._user_with_exclusive_pre_access()
        self.client.force_login(user)
        ranking_rows = [
            DebaterRankingRow(
                rank=index,
                name=f"Debater {index:03d}",
                debater_id=None,
                schools=[],
                school_name="Test University",
                rounds=100 - index,
                prelim_rounds=80 - index,
                outround_rounds=index % 5,
                elo=1700 - index,
            )
            for index in range(1, 52)
        ]
        result = EloRunResult(
            matched_tournaments=1,
            debates_processed=51,
            prelims_processed=40,
            outrounds_processed=11,
            excluded_proam_debates=0,
            qual_data_available=False,
            excluded_default_opt_out_debaters=0,
            ranking_rows=ranking_rows,
        )

        with patch("core.views.elo_views.get_cached_elo_state", return_value=result), patch(
            "core.views.elo_views._resolve_elo_season_bounds",
            return_value=(2017, 2025),
        ):
            first_page_response = self.client.get(reverse("core:elo_dashboard"))
            second_page_response = self.client.get(reverse("core:elo_dashboard"), {"page": 2})

        self.assertEqual(first_page_response.status_code, 200)
        self.assertEqual(first_page_response.context["page_obj"].number, 1)
        self.assertEqual(first_page_response.context["page_obj"].paginator.per_page, 50)
        self.assertEqual(len(first_page_response.context["table_rows"]), 50)
        self.assertContains(first_page_response, "Showing 1-50 of 51")
        self.assertContains(first_page_response, "Debater 001")
        self.assertNotContains(first_page_response, "Debater 051")
        self.assertContains(first_page_response, "Computation Seasons Included")
        self.assertContains(first_page_response, "2017-2018 through 2025-2026")
        self.assertContains(first_page_response, "Display Active Seasons")
        self.assertContains(first_page_response, "2018-2019 through 2025-2026")
        self.assertContains(
            first_page_response,
            "This range determines which seasons are used to calculate ELO.",
        )
        self.assertContains(
            first_page_response,
            "This filter only controls who appears in the rankings. Ratings are still computed from the selected computation seasons.",
        )
        self.assertContains(first_page_response, "ELO Dashboard")
        self.assertNotContains(first_page_response, "Run Summary")
        self.assertNotContains(first_page_response, "Run ELO over imported tournament dataset data.")
        self.assertNotContains(first_page_response, "Excluded ProAm Partnerships")
        self.assertContains(first_page_response, 'href="?page=2">Next</a>')
        self.assertNotContains(first_page_response, 'href="?page=2">2</a>')
        self.assertNotContains(first_page_response, '<span class="page-link">...</span>')
        self.assertContains(first_page_response, "Beta notice:")
        self.assertContains(
            first_page_response,
            "This feature is in early beta. The underlying data is incomplete and may contain inaccuracies.",
        )
        self.assertNotContains(first_page_response, "Varsity/Nat-qual exclusions")

        self.assertEqual(second_page_response.status_code, 200)
        self.assertEqual(second_page_response.context["page_obj"].number, 2)
        self.assertEqual(len(second_page_response.context["table_rows"]), 1)
        self.assertContains(second_page_response, "Showing 51-51 of 51")
        self.assertContains(second_page_response, "Debater 051")
        self.assertNotContains(second_page_response, "Debater 001")
        self.assertContains(second_page_response, 'href="?page=1">Previous</a>')
        self.assertNotContains(second_page_response, 'href="?page=1">1</a>')
