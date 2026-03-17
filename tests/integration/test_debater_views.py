# pylint: disable=import-outside-toplevel
"""
Tests for debater views
"""


from datetime import date
import csv
from io import StringIO
from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings
from django.urls import reverse

from core.models import (
    DebaterAlias,
    ImportedRoundMetadata,
    School,
    Tournament,
    Debater,
    DebaterAliasGroup,
    Team,
)
from core.models.round import Round, RoundStats
from core.models.results.speaker import SpeakerResult


class DebaterViewsTest(TestCase):
    """Test debater views"""

    def setUp(self):
        self.client = Client()
        self.school = School.objects.create(name="Test School")
        self.user = get_user_model().objects.create_user(
            username="debater-owner",
            password="testpass123",
            email="debater-owner@example.com",
        )
        self.debater = Debater.objects.create(
            first_name="John", last_name="Doe", school=self.school, user=self.user
        )
        self.tournament = Tournament.objects.create(
            name="Test Tournament",
            host=self.school,
            date=date(2024, 1, 1),
            season="2024",
        )

    def _create_debater(self, name, *, school=None, **overrides):
        first_name, _, last_name = name.partition(" ")
        return Debater.objects.create(
            first_name=first_name,
            last_name=last_name or "Test",
            school=school or self.school,
            **overrides,
        )

    def _create_team(self, name, *members, **overrides):
        team = Team.objects.create(name=name, **overrides)
        if members:
            team.debaters.add(*members)
        return team

    def _assert_shows_debater_name(self, response):
        self.assertContains(response, "John")
        self.assertContains(response, "Doe")

    def test_debater_list_view(self):
        """Test debater list view"""
        response = self.client.get(reverse("core:debater_list"))
        self.assertEqual(response.status_code, 200)
        self._assert_shows_debater_name(response)

    def test_debater_detail_view(self):
        """Test debater detail view"""
        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk}),
            {"all": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self._assert_shows_debater_name(response)

    def test_debater_with_results(self):
        """Test debater view with speaker results"""
        SpeakerResult.objects.create(
            debater=self.debater,
            tournament=self.tournament,
            type_of_place=Debater.VARSITY,
            place=5,
        )

        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk}),
            {"all": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self._assert_shows_debater_name(response)

    def test_debater_search_view(self):
        """Test debater search functionality"""
        response = self.client.get(reverse("core:debater_list"), {"search": "John"})
        self.assertEqual(response.status_code, 200)
        self._assert_shows_debater_name(response)

    def test_debater_filter_by_school(self):
        """Test filtering debaters by school"""
        # Create another school and debater
        school2 = School.objects.create(name="Other School")
        self._create_debater("Jane Smith", school=school2)

        response = self.client.get(
            reverse("core:debater_list"), {"school": self.school.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "John")
        self.assertNotContains(response, "Jane")
        self.assertContains(response, "Doe")
        self.assertNotContains(response, "Smith")

    def test_nonexistent_debater_404(self):
        """Test that non-existent debater returns 404"""
        response = self.client.get(reverse("core:debater_detail", kwargs={"pk": 99999}))
        self.assertEqual(response.status_code, 404)

    def test_debater_statistics_view(self):
        """Test debater statistics display"""
        # Create multiple results for statistics
        for i in range(3):
            SpeakerResult.objects.create(
                debater=self.debater,
                tournament=self.tournament,
                type_of_place=Debater.VARSITY,
                place=i + 1,
            )

        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk}),
            {"all": "1"},
        )
        self.assertEqual(response.status_code, 200)

    def test_debater_with_no_results(self):
        """Test debater view with no results"""
        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk})
        )
        self.assertEqual(response.status_code, 200)
        self._assert_shows_debater_name(response)

    def test_also_debated_under_section(self):
        alias_group = DebaterAliasGroup.objects.create(label="John Doe")
        self.debater.alias_group = alias_group
        self.debater.save()

        alias_school = School.objects.create(name="Alias School")
        alias = self._create_debater("John Doe", school=alias_school, alias_group=alias_group)

        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Also Debated Under")
        self.assertContains(response, alias_school.name)

    def test_debater_profile_edit_can_update_elo_override(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("core:debater_profile_edit", kwargs={"pk": self.debater.pk}),
            {
                "first_name": self.debater.first_name,
                "last_name": self.debater.last_name,
                "status": str(self.debater.status),
                "first_season": "",
                "latest_season": "",
                "elo_manual_opt": Debater.EloManualOpt.OPT_OUT,
                "paradigm": "",
                "region": [],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.debater.refresh_from_db()
        self.assertEqual(self.debater.elo_manual_opt, Debater.EloManualOpt.OPT_OUT)

    def test_debater_detail_shows_partner_breakdown_pie_chart(self):
        partner = self._create_debater("Pat Partner")
        opp_one = self._create_debater("Opp A")
        opp_two = self._create_debater("Opp B")
        team = self._create_team("Partnership", self.debater, partner)
        opponent = self._create_team("Opposition", opp_one, opp_two)

        Round.objects.create(
            gov=team,
            opp=opponent,
            tournament=self.tournament,
            round_number=1,
            victor=Round.GOV,
        )
        Round.objects.create(
            gov=opponent,
            opp=team,
            tournament=self.tournament,
            round_number=2,
            victor=Round.GOV,
        )

        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Partner Breakdown")
        self.assertContains(response, "conic-gradient(")
        self.assertContains(response, partner.name)

    def test_debater_detail_excludes_synthetic_partners_and_teams(self):
        real_partner = self._create_debater("Real Partner")
        synthetic_partner = self._create_debater("Synthetic Partner", synthetic=True)

        self._create_team("Real Partnership", self.debater, real_partner)
        self._create_team("Synthetic Partnership", self.debater, synthetic_partner, synthetic=True)

        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Real Partner")
        self.assertContains(response, "Real Partnership")
        self.assertNotContains(response, "Synthetic Partner")
        self.assertNotContains(response, "Synthetic Partnership")

    def test_partner_breakdown_excludes_synthetic_partner_on_non_synthetic_team(self):
        synthetic_partner = self._create_debater("Synthetic Partner", synthetic=True)
        opp_one = self._create_debater("Opp A")
        opp_two = self._create_debater("Opp B")

        mixed_team = self._create_team("Mixed Team", self.debater, synthetic_partner)
        opponent = self._create_team("Opposition", opp_one, opp_two)

        Round.objects.create(
            gov=mixed_team,
            opp=opponent,
            tournament=self.tournament,
            round_number=1,
            victor=Round.GOV,
        )

        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Synthetic Partner")

    def test_dino_judge_list_view(self):
        self._create_debater(
            "Judge Dino",
            status=Debater.DINO,
            dino_judge_contact_opt_in=True,
        )

        response = self.client.get(reverse("core:judge_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Judge")
        self.assertContains(response, "Dino")

    def test_dino_to_list_view(self):
        self._create_debater("Observer Dino", dino_to_contact_opt_in=True)

        response = self.client.get(reverse("core:to_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Observer")
        self.assertContains(response, "Dino")

    @override_settings(ENV="development")
    def test_debater_detail_shows_csv_button_for_any_authenticated_user_in_dev(self):
        other_user = get_user_model().objects.create_user(
            username="other-dev-user",
            password="testpass123",
            email="other-dev@example.com",
        )
        self.client.force_login(other_user)

        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Download Career Tab Cards CSV")
        self.assertContains(
            response,
            reverse("core:debater_tab_cards_csv", kwargs={"pk": self.debater.pk}),
        )

    @override_settings(ENV="production")
    def test_debater_detail_hides_csv_button_for_unlinked_user_outside_dev(self):
        other_user = get_user_model().objects.create_user(
            username="other-prod-user",
            password="testpass123",
            email="other-prod@example.com",
        )
        self.client.force_login(other_user)

        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Download Career Tab Cards CSV")

    @override_settings(ENV="development")
    def test_debater_tab_cards_csv_download_works_for_unlinked_user_in_dev(self):
        other_user = get_user_model().objects.create_user(
            username="other-dev-download",
            password="testpass123",
            email="other-dev-download@example.com",
        )
        self.client.force_login(other_user)

        response = self.client.get(
            reverse("core:debater_tab_cards_csv", kwargs={"pk": self.debater.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("career-tab-cards.csv", response["Content-Disposition"])

    def test_debater_tab_cards_csv_includes_wf_lf_and_bye_rounds_without_stats(self):
        self.client.force_login(self.user)

        partner = self._create_debater("Pat Partner")
        opp_one = self._create_debater("Opp A")
        opp_two = self._create_debater("Opp B")
        opp_three = self._create_debater("Opp C")
        opp_four = self._create_debater("Opp D")
        bye_one = self._create_debater("Bye A")
        bye_two = self._create_debater("Bye B")

        main_team = self._create_team("Main Team", self.debater, partner)
        wf_opponent = self._create_team("WF Opp", opp_one, opp_two)
        lf_opponent = self._create_team("LF Opp", opp_three, opp_four)
        bye_team = self._create_team("Bye Team", bye_one, bye_two)

        Round.objects.create(
            gov=main_team,
            opp=wf_opponent,
            tournament=self.tournament,
            round_number=1,
            victor=Round.GOV_VIA_FORFEIT,
        )
        Round.objects.create(
            gov=lf_opponent,
            opp=main_team,
            tournament=self.tournament,
            round_number=2,
            victor=Round.GOV_VIA_FORFEIT,
        )
        Round.objects.create(
            gov=main_team,
            opp=bye_team,
            tournament=self.tournament,
            round_number=3,
            victor=Round.BYE,
        )

        response = self.client.get(
            reverse("core:debater_tab_cards_csv", kwargs={"pk": self.debater.pk})
        )

        self.assertEqual(response.status_code, 200)

        rows = list(csv.DictReader(StringIO(response.content.decode("utf-8"))))
        result_codes = [row["result"] for row in rows]

        self.assertIn("WF", result_codes)
        self.assertIn("LF", result_codes)
        self.assertIn("BYE", result_codes)
