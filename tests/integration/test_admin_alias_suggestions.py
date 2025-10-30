from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from core.models import Debater, DebaterAliasGroup, School


class DebaterAliasSuggestionsViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="password",
        )
        self.client.force_login(self.user)

        self.school_one = School.objects.create(name="Alpha University")
        self.school_two = School.objects.create(name="Beta College")
        self.school_three = School.objects.create(name="Gamma Institute")

    def _create_debater(self, school, first="Alex", last="Smith", first_season="2020", latest="2024"):
        return Debater.objects.create(
            first_name=first,
            last_name=last,
            school=school,
            first_season=first_season,
            latest_season=latest,
        )

    def test_lists_potential_pairs(self):
        primary = self._create_debater(self.school_one, first_season="2020", latest="2024")
        match = self._create_debater(self.school_two, first_season="2021", latest="2023")

        # Already linked pair should not appear
        alias_group = DebaterAliasGroup.objects.create(label="Linked")
        linked_one = self._create_debater(self.school_three)
        linked_two = self._create_debater(self.school_two)
        linked_one.alias_group = alias_group
        linked_two.alias_group = alias_group
        linked_one.save()
        linked_two.save()

        url = reverse("core:debater_alias_suggestions")
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        suggestions = response.context["suggestions"]

        self.assertTrue(
            any(
                {s["debater_one"].pk, s["debater_two"].pk}
                == {primary.pk, match.pk}
                for s in suggestions
            )
        )

        self.assertFalse(
            any(
                {s["debater_one"].pk, s["debater_two"].pk}
                == {linked_one.pk, linked_two.pk}
                for s in suggestions
            )
        )

    def test_link_action_creates_alias_group(self):
        primary = self._create_debater(self.school_one)
        match = self._create_debater(self.school_two)

        url = reverse("core:debater_alias_suggestions")
        response = self.client.post(
            url,
            {"debater_one": primary.id, "debater_two": match.id},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        primary.refresh_from_db()
        match.refresh_from_db()
        self.assertIsNotNone(primary.alias_group)
        self.assertEqual(primary.alias_group, match.alias_group)

    def test_link_action_merges_existing_groups(self):
        alias_primary = DebaterAliasGroup.objects.create(label="Primary")
        alias_secondary = DebaterAliasGroup.objects.create(label="Secondary")

        primary = self._create_debater(self.school_one)
        match = self._create_debater(self.school_two)
        primary.alias_group = alias_primary
        primary.save()
        match.alias_group = alias_secondary
        match.save()

        url = reverse("core:debater_alias_suggestions")
        response = self.client.post(
            url,
            {"debater_one": primary.id, "debater_two": match.id},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        primary.refresh_from_db()
        match.refresh_from_db()
        self.assertEqual(primary.alias_group, match.alias_group)
        self.assertFalse(
            DebaterAliasGroup.objects.filter(pk=alias_secondary.pk).exists()
        )

    def test_missing_season_data_included_and_sorted(self):
        known_one = self._create_debater(
            self.school_one,
            first="Casey",
            last="Lee",
            first_season="2018",
            latest="2020",
        )
        known_two = self._create_debater(
            self.school_two,
            first="Casey",
            last="Lee",
            first_season="2019",
            latest="2021",
        )

        missing_one = self._create_debater(
            self.school_one,
            first="Riley",
            last="Kim",
            first_season=None,
            latest=None,
        )
        missing_two = self._create_debater(
            self.school_two,
            first="Riley",
            last="Kim",
            first_season=None,
            latest=None,
        )

        missing_one.first_season = None
        missing_one.latest_season = None
        missing_one.save(update_fields=["first_season", "latest_season"])
        missing_two.first_season = None
        missing_two.latest_season = None
        missing_two.save(update_fields=["first_season", "latest_season"])

        url = reverse("core:debater_alias_suggestions")
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        suggestions = response.context["suggestions"]

        sets_in_order = [
            {item["debater_one"].pk, item["debater_two"].pk}
            for item in suggestions
        ]

        self.assertIn({missing_one.pk, missing_two.pk}, sets_in_order)
        self.assertEqual(
            sets_in_order[0],
            {known_one.pk, known_two.pk},
            "Known seasons with closer overlap should be prioritized",
        )
