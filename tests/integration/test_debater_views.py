# pylint: disable=import-outside-toplevel
"""
Tests for debater views
"""


from datetime import date
import csv
from io import StringIO
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
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

    def test_synthetic_debater_hidden_from_list(self):
        self._create_debater("Synthetic Debater", synthetic=True)

        response = self.client.get(reverse("core:debater_list"))

        self.assertEqual(response.status_code, 200)
        self._assert_shows_debater_name(response)
        self.assertNotContains(response, "Synthetic Debater")

    def test_temporary_debater_is_visible_in_public_views(self):
        temporary_debater = Debater.all_objects.create(
            first_name="Temporary",
            last_name="Debater",
            school=self.school,
            temporary=True,
        )

        list_response = self.client.get(reverse("core:debater_list"))
        detail_response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": temporary_debater.pk}),
            {"all": "1"},
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, temporary_debater.first_name)
        self.assertContains(list_response, temporary_debater.last_name)
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, temporary_debater.first_name)
        self.assertContains(detail_response, temporary_debater.last_name)

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

    def test_synthetic_debater_detail_returns_404(self):
        synthetic_debater = self._create_debater("Synthetic Debater", synthetic=True)

        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": synthetic_debater.pk})
        )

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

    @override_settings(ENABLE_DEBATER_PARTNER_PIE_CHART=True)
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

    def test_debater_detail_hides_partner_breakdown_pie_chart_by_default(self):
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

        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Partner Breakdown")
        self.assertNotContains(response, "conic-gradient(")

    def test_debater_detail_hides_view_tab_card_by_default(self):
        self.client.force_login(self.user)

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

        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk}),
            {"all": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "View Tab Card")

    @override_settings(ENABLE_DEBATER_PROFILE_TAB_CARDS=True)
    def test_debater_detail_shows_view_tab_card_when_feature_enabled(self):
        self.client.force_login(self.user)

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

        response = self.client.get(
            reverse("core:debater_detail", kwargs={"pk": self.debater.pk}),
            {"all": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "View Tab Card")

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

    def test_debater_detail_shows_csv_button_for_user_with_debug_permission(self):
        other_user = get_user_model().objects.create_user(
            username="other-debug-user",
            password="testpass123",
            email="other-debug@example.com",
        )
        other_user.user_permissions.add(
            Permission.objects.get(codename="can_view_debug_tab_cards")
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

    def test_debater_detail_hides_csv_button_for_unlinked_user_without_permission(self):
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

    def test_debater_tab_cards_csv_download_works_for_user_with_debug_permission(self):
        other_user = get_user_model().objects.create_user(
            username="other-debug-download",
            password="testpass123",
            email="other-debug-download@example.com",
        )
        other_user.user_permissions.add(
            Permission.objects.get(codename="can_view_debug_tab_cards")
        )
        self.client.force_login(other_user)

        response = self.client.get(
            reverse("core:debater_tab_cards_csv", kwargs={"pk": self.debater.pk})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("career-tab-cards.csv", response["Content-Disposition"])

    def test_debater_tab_cards_csv_includes_rows_from_linked_debaters(self):
        self.client.force_login(self.user)

        alias_group = DebaterAliasGroup.objects.create(label="John Doe")
        self.debater.alias_group = alias_group
        self.debater.save()

        alias_school = School.objects.create(name="University of Chicago")
        linked_alias = self._create_debater(
            "John Doe",
            school=alias_school,
            alias_group=alias_group,
        )
        main_partner = self._create_debater("Pat Main")
        alias_partner = self._create_debater("Pat Alias", school=alias_school)
        main_opp_one = self._create_debater("Main Opp One")
        main_opp_two = self._create_debater("Main Opp Two")
        alias_opp_one = self._create_debater("Alias Opp One", school=alias_school)
        alias_opp_two = self._create_debater("Alias Opp Two", school=alias_school)

        main_team = self._create_team("Main Team", self.debater, main_partner)
        alias_team = self._create_team("Alias Team", linked_alias, alias_partner)
        main_opponent = self._create_team("Main Opponents", main_opp_one, main_opp_two)
        alias_opponent = self._create_team("Alias Opponents", alias_opp_one, alias_opp_two)

        main_round = Round.objects.create(
            gov=main_team,
            opp=main_opponent,
            tournament=self.tournament,
            round_number=1,
            victor=Round.GOV,
        )
        alias_round = Round.objects.create(
            gov=alias_team,
            opp=alias_opponent,
            tournament=self.tournament,
            round_number=2,
            victor=Round.GOV,
        )

        RoundStats.objects.create(
            round=main_round,
            debater=self.debater,
            speaks=28,
            ranks=1,
            debater_role="PM",
        )
        RoundStats.objects.create(
            round=alias_round,
            debater=linked_alias,
            speaks=27,
            ranks=2,
            debater_role="PM",
        )

        response = self.client.get(
            reverse("core:debater_tab_cards_csv", kwargs={"pk": self.debater.pk})
        )

        self.assertEqual(response.status_code, 200)

        rows = list(csv.DictReader(StringIO(response.content.decode("utf-8"))))

        self.assertEqual(len(rows), 2)
        self.assertNotIn("debater_role", rows[0])
        self.assertEqual(
            {(row["round_label"], row["partner_name"], row["opponent_1_name"]) for row in rows},
            {
                ("1", "Pat Main", "Main Opp One"),
                ("2", "Pat Alias", "Alias Opp One"),
            },
        )

    def test_debater_tab_cards_csv_keeps_outround_roles_blank_even_with_imported_aliases(self):
        self.client.force_login(self.user)

        partner = self._create_debater("Pat Partner")
        opp_one = self._create_debater("Opp A")
        opp_two = self._create_debater("Opp B")

        main_team = self._create_team("Main Team", self.debater, partner)
        opponent = self._create_team("Opp Team", opp_one, opp_two)
        outround = Round.objects.create(
            gov=main_team,
            opp=opponent,
            tournament=self.tournament,
            round_number=6,
            round_label="V04",
            stage=Round.Stage.OUTROUND,
            victor=Round.GOV,
        )

        self_alias = DebaterAlias.objects.create(
            debater=self.debater,
            source_name=self.debater.name,
            normalized_name=self.debater.name.casefold(),
        )
        partner_alias = DebaterAlias.objects.create(
            debater=partner,
            source_name=partner.name,
            normalized_name=partner.name.casefold(),
        )
        opp_one_alias = DebaterAlias.objects.create(
            debater=opp_one,
            source_name=opp_one.name,
            normalized_name=opp_one.name.casefold(),
        )
        opp_two_alias = DebaterAlias.objects.create(
            debater=opp_two,
            source_name=opp_two.name,
            normalized_name=opp_two.name.casefold(),
        )
        ImportedRoundMetadata.objects.create(
            round=outround,
            gov_1_alias=self_alias,
            gov_2_alias=partner_alias,
            opp_1_alias=opp_one_alias,
            opp_2_alias=opp_two_alias,
        )

        RoundStats.objects.create(
            round=outround,
            debater=self.debater,
            debater_role="PM",
        )

        response = self.client.get(
            reverse("core:debater_tab_cards_csv", kwargs={"pk": self.debater.pk})
        )

        self.assertEqual(response.status_code, 200)

        rows = list(csv.DictReader(StringIO(response.content.decode("utf-8"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage"], "Outround")
        self.assertNotIn("debater_role", rows[0])

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
