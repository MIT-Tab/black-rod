from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from core.models import Debater, School, Team


class RebuildSyntheticTeamNamesCommandTest(TestCase):
    def setUp(self):
        self.full_school = School.objects.create(
            name="Very Long University Name",
            short_name="VLU",
        )
        self.other_school = School.objects.create(
            name="Another College Name",
            short_name="ACN",
        )

    def test_command_rebuilds_synthetic_team_name_and_short_name(self):
        first = Debater.objects.create(
            first_name="Avery",
            last_name="Stone",
            school=self.full_school,
        )
        second = Debater.objects.create(
            first_name="Blair",
            last_name="Knight",
            school=self.other_school,
        )
        synthetic_team = Team.objects.create(
            name="Imported Source Team",
            short_name="Src Team",
            synthetic=True,
        )
        synthetic_team.debaters.set([first, second])

        canonical_team = Team.objects.create(
            name="Canonical Imported Name",
            short_name="Canonical Imported Short",
            synthetic=False,
        )
        canonical_team.debaters.set([first, second])

        stdout = StringIO()
        call_command("rebuild_synthetic_team_names", stdout=stdout)

        synthetic_team.refresh_from_db()
        canonical_team.refresh_from_db()

        self.assertEqual(
            synthetic_team.name,
            "Very Long University Name / Another College Name SK",
        )
        self.assertEqual(synthetic_team.short_name, "VLU / ACN SK")
        self.assertEqual(canonical_team.name, "Canonical Imported Name")
        self.assertEqual(canonical_team.short_name, "Canonical Imported Short")
        self.assertIn('Imported Source Team', stdout.getvalue())

    def test_command_dry_run_does_not_save_changes(self):
        first = Debater.objects.create(
            first_name="Avery",
            last_name="Stone",
            school=self.full_school,
        )
        second = Debater.objects.create(
            first_name="Casey",
            last_name="Young",
            school=self.full_school,
        )
        synthetic_team = Team.objects.create(
            name="Imported Source Team",
            short_name="Src Team",
            synthetic=True,
        )
        synthetic_team.debaters.set([first, second])

        stdout = StringIO()
        call_command("rebuild_synthetic_team_names", "--dry-run", stdout=stdout)

        synthetic_team.refresh_from_db()

        self.assertEqual(synthetic_team.name, "Imported Source Team")
        self.assertEqual(synthetic_team.short_name, "Src Team")
        self.assertIn("DRY RUN MODE", stdout.getvalue())

    def test_command_rebuilds_synthetic_team_with_only_synthetic_debaters(self):
        first = Debater.all_objects.create(
            first_name="Synthetic",
            last_name="Stone",
            school=self.full_school,
            synthetic=True,
        )
        second = Debater.all_objects.create(
            first_name="Synthetic",
            last_name="Young",
            school=self.full_school,
            synthetic=True,
        )
        synthetic_team = Team.objects.create(
            name="Imported Source Team",
            short_name="Src Team",
            synthetic=True,
        )
        synthetic_team.debaters.set([first, second])

        call_command("rebuild_synthetic_team_names")

        synthetic_team.refresh_from_db()

        self.assertEqual(synthetic_team.name, "Very Long University Name SY")
        self.assertEqual(synthetic_team.short_name, "VLU SY")
