from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings

from core.models import (
    Debater,
    School,
    SchoolLookup,
    SpeakerResult,
    Team,
    TeamResult,
    Tournament,
)
from core.utils import import_management


class ImportManagementHelpersTests(TestCase):
    def test_getters_and_counters(self):
        teams = [
            {
                "id": 1,
                "num_rounds": 6,
                "school_id": 10,
                "hybrid_school_id": None,
                "debaters": [
                    {"id": 1, "status": 0},
                    {"id": 2, "status": 1},
                ],
            },
            {
                "id": 2,
                "num_rounds": 3,
                "school_id": 11,
                "hybrid_school_id": None,
                "debaters": [
                    {"id": 3, "status": 1},
                    {"id": 4, "status": 1},
                ],
            },
        ]

        debaters = import_management.get_debaters(teams)
        self.assertEqual(len(debaters), 4)
        self.assertEqual(debaters[0]["num_rounds"], 6)
        self.assertEqual(import_management.get_num_teams(teams, num_rounds=5), 1)
        self.assertEqual(import_management.get_num_novice_debaters(teams, num_rounds=5), 1)

    def test_clean_keys_and_lookup_school(self):
        school = School.objects.create(name="Lookup School", included_in_oty=True)
        SchoolLookup.objects.create(server_name="Alt Name", school=school)

        cleaned = import_management.clean_keys({"1": "a", "2": "b"})
        self.assertEqual(cleaned, {1: "a", 2: "b"})
        self.assertEqual(import_management.lookup_school("Lookup School").id, school.id)
        self.assertEqual(import_management.lookup_school("Alt Name").id, school.id)
        self.assertIsNone(import_management.lookup_school("Missing"))


class ImportManagementMutationTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="Primary", included_in_oty=True)
        self.other_school = School.objects.create(name="Secondary", included_in_oty=True)

    def test_create_schools_handles_links_and_creates(self):
        actions = {
            "100": {"id": 100, "action": import_management.CREATE, "name": "New School"},
            "200": {
                "id": 200,
                "action": import_management.LINK,
                "school": self.school.id,
                "name": "Primary Alias",
            },
        }

        completed = import_management.create_schools(actions)

        self.assertIn("100", completed)
        self.assertTrue(School.objects.filter(id=completed["100"], name="New School").exists())
        lookup = SchoolLookup.objects.get(server_name="Primary Alias")
        self.assertEqual(lookup.school, self.school)

    def test_create_debaters_creates_and_links_records(self):
        school_actions = {10: self.other_school.id}
        existing = Debater.objects.create(first_name="Pat", last_name="Linked", school=self.school)

        debater_actions = {
            "1": {"id": 1, "action": import_management.LINK, "debater": existing.id},
            "2": {
                "id": 2,
                "action": import_management.CREATE,
                "name": "Taylor Swift",
                "status": 0,
                "school": self.school.id,
            },
            "3": {
                "id": 3,
                "action": import_management.CREATE,
                "name": "Jordan Peele",
                "status": 1,
                "school": -1,
                "school_id": 10,
            },
        }

        completed = import_management.create_debaters(school_actions, debater_actions)
        self.assertEqual(completed["1"], existing.id)
        created = Debater.objects.get(id=completed["2"])
        self.assertEqual((created.first_name, created.last_name), ("Taylor", "Swift"))
        hybrid = Debater.objects.get(id=completed["3"])
        self.assertEqual(hybrid.school, self.other_school)

    def test_create_teams_still_works(self):
        debater_one = Debater.objects.create(first_name="Alex", last_name="One", school=self.school)
        debater_two = Debater.objects.create(first_name="Blair", last_name="Two", school=self.school)
        debater_map = {1: debater_one.id, 2: debater_two.id}

        teams_payload = [{"id": 5, "debaters": [{"id": 1}, {"id": 2}]}]
        team_actions = import_management.create_teams(debater_map, teams_payload)
        team = Team.objects.get(id=team_actions[5])
        self.assertEqual(team.debaters.count(), 2)

    @override_settings(CURRENT_SEASON="2024", ONLINE_SEASONS=("2024",))
    @patch.multiple(
        import_management,
        update_soty=lambda *args, **kwargs: None,
        update_noty=lambda *args, **kwargs: None,
        redo_rankings=lambda *args, **kwargs: None,
    )
    def test_create_speaker_awards_resets_and_updates(self):
        tournament = Tournament.objects.create(
            name="Speaker Awards",
            host=self.school,
            date=date(2024, 2, 1),
            season="2024",
            num_teams=16,
        )
        debater = Debater.objects.create(first_name="Alex", last_name="One", school=self.school)
        SpeakerResult.objects.create(
            tournament=tournament,
            debater=debater,
            type_of_place=Debater.VARSITY,
            place=5,
            tie=False,
        )

        debater_actions = {"101": debater.id}
        awards = [
            {"debater": "101", "place": 1, "tie": False},
            {"debater": "101", "place": 2, "tie": False},
        ]

        import_management.create_speaker_awards(
            debater_actions, awards, Debater.VARSITY, tournament
        )
        self.assertEqual(SpeakerResult.objects.filter(tournament=tournament).count(), 2)

    @override_settings(CURRENT_SEASON="2024", ONLINE_SEASONS=("2024",))
    @patch.multiple(
        import_management,
        update_toty=lambda *args, **kwargs: None,
        rebuild_coty_related_rankings=lambda *args, **kwargs: None,
        redo_rankings=lambda *args, **kwargs: None,
    )
    def test_create_team_awards_resets_results(self):
        tournament = Tournament.objects.create(
            name="Team Awards",
            host=self.school,
            date=date(2024, 3, 1),
            season="2024",
            num_teams=16,
        )
        team = Team.objects.create(name="Existing Team")
        team.debaters.add(
            Debater.objects.create(first_name="Alex", last_name="One", school=self.school),
            Debater.objects.create(first_name="Blair", last_name="Two", school=self.school),
        )
        TeamResult.objects.create(
            tournament=tournament,
            team=team,
            type_of_place=Debater.VARSITY,
            place=8,
        )

        team_map = {55: team.id}
        awards = [
            {"team": 55, "place": 1},
            {"team": 55, "place": 2},
        ]

        import_management.create_team_awards(
            team_map, awards, Debater.VARSITY, tournament
        )

        results = TeamResult.objects.filter(tournament=tournament).order_by("place")
        self.assertEqual(results.count(), 2)
        self.assertEqual([r.place for r in results], [1, 2])
