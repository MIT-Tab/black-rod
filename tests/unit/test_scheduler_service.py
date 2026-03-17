from django.test import SimpleTestCase

from core.utils.scheduler import (
    DEFAULT_SCHEDULER_SETTINGS,
    run_scheduler,
    summarize_scheduler_inputs,
)


SCHOOLS_CSV = """\
,,,,,9/19-9/20,9/26-9/27
,,,,# of Tournaments,1,1
School Name,Region,Desired Tournaments,Priority,Tags,,
Alpha,North,1,1,,Rank 1,Rank 2
Beta,South,1,1,Unopposed,Rank 2,Rank 1
"""

DATES_CSV = """\
,# of Tournaments,Tags
9/19-9/20,1,
9/26-9/27,1,Unopposed
"""

CENTRAL_SCHOOLS_CSV = """\
,,,,,9/19-9/20
School Name,Region,Desired Tournaments,Priority,Tags,
Gamma,Central,1,1,,Rank 1
"""

TWO_TOURNAMENT_DATES_CSV = """\
,# of Tournaments,Tags
9/19-9/20,2,
"""

class SchedulerServiceTest(SimpleTestCase):
    def test_default_settings_include_central_two_tournament_penalty(self):
        self.assertEqual(
            DEFAULT_SCHEDULER_SETTINGS["central_on_two_tournament_weekend_penalty"],
            0,
        )

    def test_summarize_scheduler_inputs_counts_rows(self):
        summary = summarize_scheduler_inputs(SCHOOLS_CSV, DATES_CSV)

        self.assertEqual(summary["school_count"], 2)
        self.assertEqual(summary["date_count"], 2)
        self.assertEqual(summary["active_date_count"], 2)
        self.assertEqual(summary["scenario_count"], 1)

    def test_run_scheduler_returns_best_schedule(self):
        result = run_scheduler(
            SCHOOLS_CSV,
            DATES_CSV,
            {
                "max_workers": 1,
            },
        )

        self.assertEqual(result["best_seed"], 0)
        self.assertEqual(result["summary"]["school_count"], 2)
        self.assertEqual(len(result["schedule"]), 2)
        self.assertEqual(result["unmatched_schools"], [])
        self.assertEqual(result["summary"]["scenario_count"], 1)
        self.assertIn("Total Penalty:", result["output_text"])

    def test_central_two_tournament_penalty_overrides_directional_penalties(self):
        result = run_scheduler(
            CENTRAL_SCHOOLS_CSV,
            TWO_TOURNAMENT_DATES_CSV,
            {
                "max_workers": 1,
                "rank_1_penalty": 0,
                "central_to_north_penalty": -1000,
                "central_to_south_penalty": -1000,
                "central_on_two_tournament_weekend_penalty": -321,
            },
        )

        assignment = result["schedule"][0]["assignments"][0]
        self.assertIn(assignment["region"], {"North", "South"})
        self.assertEqual(assignment["weight"], 9679)
        self.assertEqual(assignment["penalties"]["region"], -321)
