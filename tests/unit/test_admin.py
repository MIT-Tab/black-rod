
from datetime import date
from django.contrib import admin as django_admin
from django.test import TestCase
from django.contrib.admin.sites import AdminSite
from django.urls import reverse


from core.models.school import School
from core.models.debater_alias import DebaterAlias
from core.models.debater import Debater
from core.models.debater_alias_group import DebaterAliasGroup
from core.models.immaculate_grid import ImmaculateGridHiddenDebater
from core.models.round import Round, RoundStats
from core.models.round_import import (
    ImportBatch,
    ImportedRoundJudge,
    ImportedRoundMetadata,
    TournamentImport,
)
from core.models.standings.online_qual import OnlineQUAL
from core.models.tags import TaggedResource
from core.models.team import Team
from core.models.tournament import Tournament
from core.models.user import User
from core.utils.immaculate_grid.catalog import hidden_people, person_key



class MockRequest:
    """Mock request object for admin testing"""

    def __init__(self, user=None):
        self.user = user


class AdminTestCase(TestCase):
    """Test admin interface functionality"""

    def setUp(self):
        self.site = AdminSite()
        self.superuser = User.objects.create_superuser(
            username="admin", email="admin@test.com", password="admin123"
        )



    def test_model_string_representations_for_admin(self):
        """Test model __str__ methods work for admin display"""
        school = School.objects.create(name="Admin Test School")
        self.assertEqual(str(school), "Admin Test School")

        debater = Debater.objects.create(
            first_name="Admin", last_name="Test", school=school
        )
        self.assertEqual(str(debater), "Admin Test")

        tournament = Tournament.objects.create(
            name="Admin Test Tournament", host=school, date=date.today()
        )
        # Tournament name gets set to host name during save
        self.assertEqual(str(tournament), "Admin Test School")

    def test_admin_list_display_fields_exist(self):
        """Test that common admin list display fields exist on models"""
        school = School.objects.create(name="Field Test School")

        # Test that commonly used admin fields exist
        self.assertTrue(hasattr(school, "name"))
        self.assertTrue(hasattr(school, "included_in_oty"))

        debater = Debater.objects.create(
            first_name="Field", last_name="Test", school=school
        )

        self.assertTrue(hasattr(debater, "first_name"))
        self.assertTrue(hasattr(debater, "last_name"))
        self.assertTrue(hasattr(debater, "school"))
        self.assertTrue(hasattr(debater, "status"))

    def test_admin_search_fields_work(self):
        """Test that potential admin search fields work"""
        school = School.objects.create(name="Search Test School")

        # Test searching by name
        schools = School.objects.filter(name__icontains="Search")
        self.assertIn(school, schools)

        debater = Debater.objects.create(
            first_name="Search", last_name="Test", school=school
        )

        # Test searching by first name
        debaters = Debater.objects.filter(first_name__icontains="Search")
        self.assertIn(debater, debaters)

        # Test searching by last name
        debaters = Debater.objects.filter(last_name__icontains="Test")
        self.assertIn(debater, debaters)

    def test_admin_filter_fields_work(self):
        """Test that potential admin filter fields work"""
        school1 = School.objects.create(name="Filter School 1", included_in_oty=True)
        school2 = School.objects.create(name="Filter School 2", included_in_oty=False)

        # Test filtering by included_in_oty
        oty_schools = School.objects.filter(included_in_oty=True)
        self.assertIn(school1, oty_schools)
        self.assertNotIn(school2, oty_schools)

        debater1 = Debater.objects.create(
            first_name="Filter1",
            last_name="Test",
            school=school1,
            status=Debater.VARSITY,
        )
        debater2 = Debater.objects.create(
            first_name="Filter2",
            last_name="Test",
            school=school2,
            status=Debater.NOVICE,
        )

        # Test filtering by status
        varsity_debaters = Debater.objects.filter(status=Debater.VARSITY)
        self.assertIn(debater1, varsity_debaters)
        self.assertNotIn(debater2, varsity_debaters)

        # Test filtering by school
        school1_debaters = Debater.objects.filter(school=school1)
        self.assertIn(debater1, school1_debaters)
        self.assertNotIn(debater2, school1_debaters)

    def test_new_models_are_registered_in_admin(self):
        registered_models = django_admin.site._registry

        for model in (
            DebaterAlias,
            ImportBatch,
            TournamentImport,
            ImportedRoundMetadata,
            ImportedRoundJudge,
            OnlineQUAL,
            TaggedResource,
        ):
            with self.subTest(model=model.__name__):
                self.assertIn(model, registered_models)

    def test_debater_admin_includes_synthetic_temporary_rows(self):
        school = School.objects.create(name="Synthetic Admin School")
        synthetic = Debater.all_objects.create(
            first_name="Synthetic",
            last_name="Person",
            school=school,
            synthetic=True,
            temporary=True,
        )

        self.assertFalse(Debater.objects.filter(pk=synthetic.pk).exists())

        model_admin = django_admin.site._registry[Debater]
        queryset = model_admin.get_queryset(MockRequest(self.superuser))

        self.assertIn(synthetic, queryset)

    def test_debater_admin_checkbox_sets_and_clears_hidden_status_for_linked_names(self):
        group = DebaterAliasGroup.objects.create()
        debater = Debater.objects.create(first_name="Hidden", last_name="Person", alias_group=group)
        linked = Debater.objects.create(first_name="Other", last_name="Name", alias_group=group)
        self.client.force_login(self.superuser)
        url = reverse("admin:core_debater_change", args=[debater.pk])
        payload = {
            "first_name": debater.first_name, "last_name": debater.last_name,
            "status": debater.status, "alias_group": group.pk,
            "immaculate_hidden": "on", "_save": "Save",
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ImmaculateGridHiddenDebater.objects.filter(debater=debater).exists())
        self.assertIn(person_key(linked.pk, linked.alias_group_id), hidden_people())

        linked_url = reverse("admin:core_debater_change", args=[linked.pk])
        response = self.client.get(linked_url)
        self.assertTrue(response.context["adminform"].form["immaculate_hidden"].value())
        payload.update(first_name=linked.first_name, last_name=linked.last_name)
        del payload["immaculate_hidden"]
        response = self.client.post(linked_url, payload)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ImmaculateGridHiddenDebater.objects.filter(debater__alias_group=group).exists())

    def test_linking_a_profile_does_not_accidentally_unhide_the_group(self):
        group = DebaterAliasGroup.objects.create()
        hidden = Debater.objects.create(first_name="Hidden", alias_group=group)
        other = Debater.objects.create(first_name="Other")
        ImmaculateGridHiddenDebater.objects.create(debater=hidden)
        self.client.force_login(self.superuser)
        response = self.client.post(reverse("admin:core_debater_change", args=[other.pk]), {
            "first_name": other.first_name, "status": other.status,
            "alias_group": group.pk, "_save": "Save",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ImmaculateGridHiddenDebater.objects.filter(debater=hidden).exists())

    def test_hidden_debater_admin_uses_searchable_lookup_instead_of_full_dropdown(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin:core_immaculategridhiddendebater_add"))
        self.assertContains(response, 'class="vForeignKeyRawIdAdminField"')
        self.assertContains(response, 'class="related-lookup"')
        self.assertNotContains(response, '<select name="debater"')

    def test_round_admin_queryset_includes_stats_count_and_import_summary(self):
        round_obj, gov_one = self._create_round_fixture()
        alias = DebaterAlias.objects.create(
            source_name="Pat Prime",
            normalized_name="pat prime",
            debater=gov_one,
        )
        ImportedRoundMetadata.objects.create(
            round=round_obj,
            gov_1_alias=alias,
            raw_result_code="2-1",
            raw_outcome_text="Government won on a split decision.",
        )

        model_admin = django_admin.site._registry[Round]
        row = model_admin.get_queryset(MockRequest(self.superuser)).get(pk=round_obj.pk)

        self.assertEqual(row._stats_count, 2)
        self.assertEqual(model_admin.round_summary(row), "Quarterfinal (#3)")
        self.assertIn("Result code: 2-1", model_admin.imported_metadata_summary(row))

    def test_round_admin_changelist_shows_round_context(self):
        round_obj, _ = self._create_round_fixture()
        self.client.force_login(self.superuser)

        response = self.client.get(reverse("admin:core_round_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, round_obj.tournament.name)
        self.assertContains(response, "Quarterfinal (#3)")
        self.assertContains(response, round_obj.gov.name)
        self.assertContains(response, round_obj.opp.name)

    def _create_round_fixture(self):
        school = School.objects.create(name="Round Admin School", short_name="RAS")
        tournament = Tournament.objects.create(
            host=school,
            date=date.today(),
            season="2025",
            manual_name="Round Admin Invitational",
        )
        gov_one = Debater.objects.create(
            first_name="Pat",
            last_name="Prime",
            school=school,
        )
        gov_two = Debater.objects.create(
            first_name="Morgan",
            last_name="Gov",
            school=school,
        )
        opp_one = Debater.objects.create(
            first_name="Lee",
            last_name="Opp",
            school=school,
        )
        opp_two = Debater.objects.create(
            first_name="Mika",
            last_name="Opp",
            school=school,
        )
        gov = Team.objects.create(name="Round Admin Gov", short_name="RAG")
        opp = Team.objects.create(name="Round Admin Opp", short_name="RAO")
        gov.debaters.add(gov_one, gov_two)
        opp.debaters.add(opp_one, opp_two)
        round_obj = Round.objects.create(
            tournament=tournament,
            gov=gov,
            opp=opp,
            round_number=3,
            round_label="Quarterfinal",
            stage=Round.Stage.OUTROUND,
            elim_size=8,
            victor=Round.GOV,
            import_origin="forum_post",
            metadata={"source_round_name": "QF Source"},
        )
        RoundStats.objects.create(
            round=round_obj,
            debater=gov_one,
            debater_role="PM",
            speaks=28,
            ranks=1,
        )
        RoundStats.objects.create(
            round=round_obj,
            debater=opp_one,
            debater_role="LO",
            speaks=27,
            ranks=2,
        )
        return round_obj, gov_one





def test_model_verbose_names():
    """Test model verbose names for admin display"""
    # Test that models have reasonable verbose names
    assert School._meta.verbose_name
    assert Debater._meta.verbose_name
    assert Tournament._meta.verbose_name
