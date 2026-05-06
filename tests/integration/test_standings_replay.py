from datetime import date

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from core.models.debater import Debater, QualPoints
from core.models.results.speaker import SpeakerResult
from core.models.results.team import TeamResult
from core.models.school import School
from core.models.standings.coty import COTY
from core.models.standings.qual import QUAL
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
        self.assertEqual(payload["marker_limits"]["coty"], 0)

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

        coty_entries = payload["standings"]["coty"]
        self.assertEqual(len(coty_entries), 1)
        coty_markers = coty_entries[0]["all_markers"]
        self.assertEqual(
            {marker["marker_type"] for marker in coty_markers},
            {"qual_points", "qual_bonus"},
        )
        self.assertEqual(
            len([marker for marker in coty_markers if marker["marker_type"] == "qual_bonus"]),
            2,
        )
        self.assertEqual(
            {marker["earned_on"] for marker in coty_markers},
            {
                self.tournament_one.date.isoformat(),
                self.tournament_two.date.isoformat(),
            },
        )

    def test_standings_snapshot_through_date_limits_markers(self):
        """The partial standings endpoint should only count markers earned on/before the target date."""
        url = reverse("api:standings_through_date")
        target_date = self.tournament_one.date.isoformat()
        response = self.client.get(
            f"{url}?season={self.season}&through={target_date}"
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["through"], target_date)
        toty_rows = payload["standings"]["toty"]
        self.assertEqual(len(toty_rows), 1)
        first_marker_points = self.tournament_one.get_toty_points(1)
        self.assertAlmostEqual(toty_rows[0]["points"], first_marker_points, places=2)
        self.assertEqual(len(toty_rows[0]["markers"]), 1)
        self.assertEqual(
            toty_rows[0]["markers"][0]["earned_on"], target_date
        )

        soty_rows = payload["standings"]["soty"]
        self.assertEqual(len(soty_rows), 1)
        first_soty_points = self.tournament_one.get_soty_points(1)
        self.assertAlmostEqual(soty_rows[0]["points"], first_soty_points, places=2)
        self.assertEqual(len(soty_rows[0]["markers"]), 1)

        coty_rows = payload["standings"]["coty"]
        self.assertEqual(len(coty_rows), 1)
        first_coty_points = 2 * (
            self.tournament_one.get_qual_points(1) + 6
        )
        self.assertAlmostEqual(coty_rows[0]["points"], first_coty_points, places=2)
        self.assertEqual(
            len([marker for marker in coty_rows[0]["markers"] if marker["marker_type"] == "qual_bonus"]),
            2,
        )

    def test_standings_snapshot_requires_valid_date(self):
        """API returns 400 when through parameter missing or malformed."""
        url = reverse("api:standings_through_date")
        missing_response = self.client.get(f"{url}?season={self.season}")
        self.assertEqual(missing_response.status_code, 400)

        bad_response = self.client.get(
            f"{url}?season={self.season}&through=not-a-date"
        )
        self.assertEqual(bad_response.status_code, 400)

    def test_replay_api_board_limit_filters(self):
        url = reverse("api:season_standings_replay")
        response = self.client.get(
            f"{url}?season={self.season}&board=toty&limit=1"
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(list(payload["standings"].keys()), ["toty"])
        self.assertEqual(len(payload["standings"]["toty"]), 1)

    def test_coty_replay_is_read_only_for_persisted_rankings(self):
        cache.clear()
        url = reverse("api:season_standings_replay")
        before = {
            "coty": list(COTY.objects.values_list("id", "points", "place", "tied")),
            "quals": QUAL.objects.count(),
            "qual_points": QualPoints.objects.count(),
        }

        response = self.client.get(f"{url}?season={self.season}&board=coty")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["standings"]["coty"]), 1)

        after = {
            "coty": list(COTY.objects.values_list("id", "points", "place", "tied")),
            "quals": QUAL.objects.count(),
            "qual_points": QualPoints.objects.count(),
        }
        self.assertEqual(after, before)

    def test_replay_page_accepts_coty_default(self):
        url = reverse("core:standings_replay")
        response = self.client.get(f"{url}?season={self.season}&default=coty")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Club of the Year")
        self.assertContains(response, "replay-coty-body")

    def test_team_detail_limit_parameter(self):
        url = reverse("api:team_detail", args=[self.team.id])
        response = self.client.get(f"{url}?limit=1")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["tournaments"]), 1)
        self.assertEqual(len(payload["toty_history"]), 1)
