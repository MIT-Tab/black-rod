import json
from datetime import date
from io import StringIO
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase

from core.models import (
    Debater,
    DebaterAliasGroup,
    School,
    SchoolLookup,
    SpeakerResult,
    Team,
    TeamResult,
    Tournament,
)


class ExportCanonicalImportDataCommandTest(TestCase):
    def setUp(self):
        self.host = School.objects.create(name="Host University", short_name="Host")
        self.other_school = School.objects.create(name="Other College", short_name="Other")
        SchoolLookup.objects.create(server_name="hostu", school=self.host)

        self.alias_group = DebaterAliasGroup.objects.create(label="Alex Smith")
        self.alex = Debater.objects.create(
            first_name="Alex",
            last_name="Smith",
            school=self.host,
            first_season="2021",
            latest_season="2024",
            alias_group=self.alias_group,
        )
        self.blair = Debater.objects.create(
            first_name="Blair",
            last_name="Jones",
            school=self.other_school,
            first_season="2021",
            latest_season="2024",
        )
        self.old_same_name = Debater.objects.create(
            first_name="Alex",
            last_name="Smith",
            school=self.other_school,
            first_season="2018",
            latest_season="2020",
        )
        self.alias_link = Debater.objects.create(
            first_name="Alexander",
            last_name="Smith",
            school=self.host,
            first_season="2019",
            latest_season="2020",
            alias_group=self.alias_group,
        )
        self.old_debater = Debater.objects.create(
            first_name="Old",
            last_name="Timer",
            school=self.host,
            first_season="2020",
            latest_season="2020",
        )

        self.team = Team.objects.create(name="Host University SJ", short_name="Host SJ")
        self.team.debaters.set([self.alex, self.blair])
        self.old_team = Team.objects.create(name="Host University OT", short_name="Host OT")
        self.old_team.debaters.set([self.old_debater])

        self.old_tournament = Tournament.objects.create(
            name="Old Open",
            manual_name="Old Open",
            host=self.host,
            date=date(2021, 2, 1),
            season="2020",
        )
        self.new_tournament = Tournament.objects.create(
            name="New Open",
            manual_name="New Open",
            host=self.host,
            date=date(2022, 2, 1),
            season="2021",
        )

        SpeakerResult.objects.create(
            tournament=self.old_tournament,
            debater=self.old_debater,
            type_of_place=SpeakerResult.VARSITY,
            place=1,
        )
        SpeakerResult.objects.create(
            tournament=self.new_tournament,
            debater=self.alex,
            type_of_place=SpeakerResult.VARSITY,
            place=2,
            tie=True,
        )
        TeamResult.objects.create(
            tournament=self.old_tournament,
            team=self.old_team,
            type_of_place=TeamResult.VARSITY,
            place=1,
        )
        TeamResult.objects.create(
            tournament=self.new_tournament,
            team=self.team,
            type_of_place=TeamResult.VARSITY,
            place=3,
            ghost_points=True,
        )

    def test_command_exports_filtered_canonical_payload(self):
        with TemporaryDirectory() as tmpdir:
            output_path = f"{tmpdir}/canonical.json"
            stdout = StringIO()

            call_command(
                "export_canonical_import_data",
                "--starting-season",
                "2021",
                "--output",
                output_path,
                stdout=stdout,
            )

            payload = json.loads(open(output_path, encoding="utf-8").read())

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["starting_season"], "2021")
        self.assertEqual(payload["counts"]["tournaments"], 1)
        self.assertEqual(payload["counts"]["seed_debaters"], 2)
        self.assertEqual(payload["counts"]["debaters"], 4)
        self.assertEqual(payload["counts"]["teams"], 1)

        self.assertEqual([t["id"] for t in payload["tournaments"]], [self.new_tournament.id])
        tournament_payload = payload["tournaments"][0]
        self.assertEqual(tournament_payload["name"], "New Open")
        self.assertEqual(tournament_payload["host_id"], self.host.id)
        self.assertEqual(tournament_payload["host_profile_path"], self.host.get_absolute_url())
        self.assertEqual(tournament_payload["profile_path"], self.new_tournament.get_absolute_url())

        self.assertEqual(len(tournament_payload["speaker_results"]), 1)
        speaker_result = tournament_payload["speaker_results"][0]
        self.assertEqual(speaker_result["debater_id"], self.alex.id)
        self.assertEqual(speaker_result["debater_name"], self.alex.name)
        self.assertEqual(speaker_result["debater_school_name"], self.host.name)
        self.assertEqual(speaker_result["debater_profile_path"], self.alex.get_absolute_url())
        self.assertEqual(speaker_result["debater_first_season"], "2021")
        self.assertEqual(speaker_result["debater_latest_season"], "2024")
        self.assertTrue(speaker_result["debater_in_export"])
        self.assertTrue(speaker_result["tie"])

        self.assertEqual(len(tournament_payload["team_results"]), 1)
        team_result = tournament_payload["team_results"][0]
        self.assertEqual(team_result["team_id"], self.team.id)
        self.assertEqual(team_result["team_name"], self.team.name)
        self.assertEqual(team_result["team_profile_path"], self.team.get_absolute_url())
        self.assertTrue(team_result["ghost_points"])
        self.assertEqual(
            [debater["id"] for debater in team_result["debaters"]],
            [self.alex.id, self.blair.id],
        )
        self.assertEqual(
            [school["name"] for school in payload["schools"]],
            [self.host.name, self.other_school.name],
        )

        debaters_by_id = {debater["id"]: debater for debater in payload["debaters"]}
        self.assertEqual(
            sorted(debaters_by_id),
            sorted([self.alex.id, self.blair.id, self.old_same_name.id, self.alias_link.id]),
        )
        self.assertEqual(debaters_by_id[self.alex.id]["school_name"], self.host.name)
        self.assertEqual(debaters_by_id[self.alex.id]["year_start"], "2021")
        self.assertEqual(debaters_by_id[self.alex.id]["year_end"], "2024")
        self.assertEqual(debaters_by_id[self.alex.id]["profile_path"], self.alex.get_absolute_url())
        self.assertEqual(
            debaters_by_id[self.alex.id]["same_name_debater_ids"],
            [self.alex.id, self.old_same_name.id],
        )
        self.assertEqual(
            debaters_by_id[self.alex.id]["linked_debater_ids"],
            [self.alex.id, self.alias_link.id],
        )
        self.assertEqual(
            debaters_by_id[self.alias_link.id]["linked_debater_ids"],
            [self.alex.id, self.alias_link.id],
        )

        self.assertEqual([lookup["server_name"] for lookup in payload["school_lookups"]], ["hostu"])
        self.assertEqual(
            [alias_group["id"] for alias_group in payload["debater_alias_groups"]],
            [self.alias_group.id],
        )
        self.assertIn("Exported canonical import data", stdout.getvalue())
