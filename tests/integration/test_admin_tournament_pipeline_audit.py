from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from core.models import (
    Debater,
    ImportedRoundMetadata,
    Round,
    RoundStats,
    School,
    Team,
    TeamResult,
    Tournament,
    TournamentImport,
)


class TournamentPipelineAuditViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.superuser = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="password",
        )
        self.regular_user = get_user_model().objects.create_user(
            username="user",
            email="user@example.com",
            password="password",
        )

        self.school = School.objects.create(name="Audit University")
        self.tournament_2016 = Tournament.objects.create(
            name="Legacy Tournament",
            host=self.school,
            date=date(2016, 10, 1),
            season="2016",
            num_rounds=5,
            num_teams=24,
            qual_type=Tournament.POINTS,
        )
        self.tournament = Tournament.objects.create(
            name="Audit Tournament",
            host=self.school,
            date=date(2017, 10, 1),
            season="2017",
            num_rounds=2,
            num_teams=4,
            qual_type=Tournament.POINTS,
        )
        self.online_no_points = Tournament.objects.create(
            name="Online No Points",
            host=self.school,
            date=date(2017, 10, 2),
            season="2017",
            num_rounds=5,
            num_teams=20,
            qual_type=Tournament.ONLINE,
        )
        self.zero_team_tournament = Tournament.objects.create(
            name="Zero Team Event",
            host=self.school,
            date=date(2017, 10, 3),
            season="2017",
            num_rounds=5,
            num_teams=0,
            qual_type=Tournament.POINTS,
        )

        self.team_a = self._create_team("Team A")
        self.team_b = self._create_team("Team B")
        self.team_c = self._create_team("Team C")
        self.team_d = self._create_team("Team D")

    def _create_team(self, name):
        team = Team.objects.create(name=name, short_name=name)
        team.debaters.add(
            Debater.objects.create(
                first_name=f"{name} Debater",
                last_name="One",
                school=self.school,
            ),
            Debater.objects.create(
                first_name=f"{name} Debater",
                last_name="Two",
                school=self.school,
            ),
        )
        return team

    def _create_round(self, *, tournament, gov, opp, round_number, stage, import_origin, label):
        round_obj = Round.objects.create(
            gov=gov,
            opp=opp,
            tournament=tournament,
            round_number=round_number,
            stage=stage,
            import_origin=import_origin,
            round_label=label,
            victor=Round.GOV,
            is_rated=True,
            metadata={"source_round_name": f"Source {label}"},
        )
        RoundStats.objects.create(
            round=round_obj,
            debater=gov.debaters.order_by("id").first(),
            debater_role="PM",
            speaks=Decimal("27.5"),
            ranks=Decimal("1.0"),
            metadata={"speaker_name": "Source PM"},
        )
        return round_obj

    def test_superuser_can_view_completeness_rows(self):
        TournamentImport.objects.create(
            tournament=self.tournament,
            import_type=TournamentImport.ImportType.FILE_BACKUP,
            original_file_name="audit.json",
        )
        self._create_round(
            tournament=self.tournament,
            gov=self.team_a,
            opp=self.team_b,
            round_number=1,
            stage=Round.Stage.PRELIM,
            import_origin="forum_post",
            label="P1",
        )
        self._create_round(
            tournament=self.tournament,
            gov=self.team_c,
            opp=self.team_d,
            round_number=2,
            stage=Round.Stage.PRELIM,
            import_origin="manual",
            label="P2",
        )
        self._create_round(
            tournament=self.tournament,
            gov=self.team_a,
            opp=self.team_c,
            round_number=3,
            stage=Round.Stage.OUTROUND,
            import_origin="forum_post",
            label="Final",
        )
        for place, team in enumerate(
            [self.team_a, self.team_b, self.team_c, self.team_d],
            start=1,
        ):
            TeamResult.objects.create(
                tournament=self.tournament,
                team=team,
                type_of_place=TeamResult.VARSITY,
                place=place,
            )

        self.client.force_login(self.superuser)
        response = self.client.get(reverse("core:tournament_pipeline_audit"))
        self.assertEqual(response.status_code, 200)

        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["tournament_id"], self.tournament.id)
        self.assertEqual(row["recorded_prelim"], 2)
        self.assertEqual(row["expected_prelim"], 4)
        self.assertEqual(row["recorded_outround"], 1)
        self.assertEqual(row["expected_outround"], 3)
        self.assertEqual(row["total_pct"], 42.9)
        self.assertEqual(row["imported_round_count"], 2)
        self.assertEqual(row["manual_round_count"], 1)
        self.assertEqual(row["linked_import_count"], 1)
        self.assertNotEqual(row["tournament_id"], self.online_no_points.id)
        self.assertNotEqual(row["tournament_id"], self.zero_team_tournament.id)
        self.assertContains(response, "Tournament Data Audit")
        self.assertContains(response, "Show Incomplete Only")

    def test_non_superuser_cannot_access(self):
        self.client.force_login(self.regular_user)
        response = self.client.get(reverse("core:tournament_pipeline_audit"))
        self.assertEqual(response.status_code, 403)

    def test_tournament_audit_detail_lists_rounds_and_modify_links(self):
        round_obj = self._create_round(
            tournament=self.tournament,
            gov=self.team_a,
            opp=self.team_b,
            round_number=1,
            stage=Round.Stage.PRELIM,
            import_origin="forum_post",
            label="P1",
        )

        self.client.force_login(self.superuser)
        response = self.client.get(
            reverse("core:tournament_audit_detail", kwargs={"pk": self.tournament.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.tournament.name)
        self.assertContains(response, "Modify Round")
        self.assertContains(
            response,
            reverse(
                "core:tournament_audit_round_edit",
                kwargs={"tournament_id": self.tournament.id, "round_id": round_obj.id},
            ),
        )
        self.assertContains(response, "Forum Post")

    def test_round_edit_updates_ballot_data(self):
        original_round = self._create_round(
            tournament=self.tournament,
            gov=self.team_a,
            opp=self.team_b,
            round_number=1,
            stage=Round.Stage.PRELIM,
            import_origin="forum_post",
            label="P1",
        )
        ImportedRoundMetadata.objects.create(round=original_round)

        pm, mg = list(self.team_c.debaters.order_by("id"))
        lo, mo = list(self.team_d.debaters.order_by("id"))

        self.client.force_login(self.superuser)
        response = self.client.post(
            reverse(
                "core:tournament_audit_round_edit",
                kwargs={"tournament_id": self.tournament.id, "round_id": original_round.id},
            ),
            {
                "canonical_round_name": "Quarterfinal",
                "source_round_name": "QF Source",
                "stage": Round.Stage.OUTROUND,
                "round_number": 3,
                "victor": Round.OPP,
                "is_rated": "on",
                "weight": "1.5",
                "pm_debater": pm.id,
                "pm_source_name": "Pat Prime",
                "pm_speaks": "28.5",
                "pm_ranks": "1",
                "mg_debater": mg.id,
                "mg_source_name": "Morgan Gov",
                "mg_speaks": "28.0",
                "mg_ranks": "2",
                "lo_debater": lo.id,
                "lo_source_name": "Lee Opp",
                "lo_speaks": "29.0",
                "lo_ranks": "1",
                "mo_debater": mo.id,
                "mo_source_name": "Mika Opp",
                "mo_speaks": "28.0",
                "mo_ranks": "2",
            },
        )
        self.assertEqual(response.status_code, 302)

        original_round.refresh_from_db()
        self.assertEqual(original_round.round_label, "Quarterfinal")
        self.assertEqual(original_round.stage, Round.Stage.OUTROUND)
        self.assertEqual(original_round.round_number, 3)
        self.assertEqual(original_round.victor, Round.OPP)
        self.assertEqual(original_round.weight, 1.5)
        self.assertEqual(original_round.metadata["source_round_name"], "QF Source")
        self.assertEqual(original_round.metadata["team_a_names"], ["Pat Prime", "Morgan Gov"])
        self.assertEqual(original_round.metadata["team_b_names"], ["Lee Opp", "Mika Opp"])
        self.assertEqual(set(original_round.gov.debaters.values_list("id", flat=True)), {pm.id, mg.id})
        self.assertEqual(set(original_round.opp.debaters.values_list("id", flat=True)), {lo.id, mo.id})

        stats = {
            stat.debater_role: stat
            for stat in original_round.stats.select_related("debater").all()
        }
        self.assertEqual(stats["PM"].metadata["speaker_name"], "Pat Prime")
        self.assertEqual(str(stats["PM"].speaks), "28.5000")
        self.assertEqual(stats["LO"].metadata["speaker_name"], "Lee Opp")

        imported_metadata = original_round.imported_metadata
        self.assertEqual(imported_metadata.gov_1_alias.source_name, "Pat Prime")
        self.assertEqual(imported_metadata.gov_2_alias.source_name, "Morgan Gov")
        self.assertEqual(imported_metadata.opp_1_alias.source_name, "Lee Opp")
        self.assertEqual(imported_metadata.opp_2_alias.source_name, "Mika Opp")
