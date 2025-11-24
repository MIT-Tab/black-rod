from datetime import date

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from core.models.debater import Debater
from core.models.results.speaker import SpeakerResult
from core.models.results.team import TeamResult
from core.models.school import School
from core.models.standings.soty import SOTY
from core.models.standings.toty import TOTY
from core.models.team import Team
from core.models.tournament import Tournament


class StandingsReplayAPITests(TestCase):
    def setUp(self):
        self.season = settings.CURRENT_SEASON
        self.school = School.objects.create(name="Replay University", included_in_oty=True)
        self.debaters = [
            Debater.objects.create(first_name="Alex", last_name="One", school=self.school),
            Debater.objects.create(first_name="Bryn", last_name="Two", school=self.school),
        ]
        self.team = Team.objects.create(name="")
        self.team.debaters.add(*self.debaters)
        self.team.update_name()
        self.team.save()

        self.tournament_one = Tournament.objects.create(
            name="Replay Open",
            manual_name="Replay Open",
            host=self.school,
            date=date(2024, 9, 15),
            season=self.season,
            num_teams=32,
            num_debaters=64,
            num_novice_debaters=0,
        )
        self.tournament_two = Tournament.objects.create(
            name="Replay Classic",
            manual_name="Replay Classic",
            host=self.school,
            date=date(2024, 10, 6),
            season=self.season,
            num_teams=40,
            num_debaters=80,
            num_novice_debaters=0,
        )

        TeamResult.objects.create(
            tournament=self.tournament_one,
            team=self.team,
            place=1,
            type_of_place=Debater.VARSITY,
        )
        TeamResult.objects.create(
            tournament=self.tournament_two,
            team=self.team,
            place=2,
            type_of_place=Debater.VARSITY,
        )

        SpeakerResult.objects.create(
            tournament=self.tournament_one,
            debater=self.debaters[0],
            place=1,
            type_of_place=Debater.VARSITY,
        )
        SpeakerResult.objects.create(
            tournament=self.tournament_two,
            debater=self.debaters[0],
            place=5,
            type_of_place=Debater.VARSITY,
        )

        self.toty = TOTY.objects.create(
            season=self.season,
            team=self.team,
            points=30,
            marker_one=18,
            marker_two=12,
            tournament_one=self.tournament_one,
            tournament_two=self.tournament_two,
        )
        self.soty = SOTY.objects.create(
            season=self.season,
            debater=self.debaters[0],
            points=24,
            marker_one=15,
            marker_two=9,
            tournament_one=self.tournament_one,
            tournament_two=self.tournament_two,
        )

    def test_replay_api_returns_all_markers_and_timeline(self):
        url = reverse("api:season_standings_replay")
        response = self.client.get(f"{url}?season={self.season}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["season"], self.season)
        self.assertIn("timeline_dates", payload)
        self.assertEqual(
            payload["timeline_dates"],
            sorted(
                {
                    self.tournament_one.date.isoformat(),
                    self.tournament_two.date.isoformat(),
                }
            ),
        )
        self.assertIn("marker_limits", payload)
        self.assertEqual(payload["marker_limits"]["toty"], 5)
        self.assertEqual(payload["marker_limits"]["soty"], 6)

        toty_entries = payload["standings"]["toty"]
        self.assertEqual(len(toty_entries), 1)
        toty_markers = toty_entries[0]["all_markers"]
        self.assertEqual(len(toty_markers), 2)
        self.assertEqual(
            {marker["earned_on"] for marker in toty_markers},
            {
                self.tournament_one.date.isoformat(),
                self.tournament_two.date.isoformat(),
            },
        )

        soty_entries = payload["standings"]["soty"]
        self.assertEqual(len(soty_entries), 1)
        soty_markers = soty_entries[0]["all_markers"]
        self.assertEqual(len(soty_markers), 2)
        self.assertEqual(
            {marker["earned_on"] for marker in soty_markers},
            {
                self.tournament_one.date.isoformat(),
                self.tournament_two.date.isoformat(),
            },
        )
