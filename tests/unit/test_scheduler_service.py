from django.test import SimpleTestCase

from core.utils.scheduler import (
    DEFAULT_SCHEDULER_SETTINGS,
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
