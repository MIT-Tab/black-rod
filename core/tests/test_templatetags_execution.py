# pylint: disable=import-outside-toplevel
from unittest.mock import Mock, patch
from decimal import Decimal
import pytest
from django.test import TestCase

from core.templatetags.tags import register


class TemplateTagExecutionTest(TestCase):
    """Test template tag execution paths"""

    def test_wl_filter_execution_paths(self):
        """Test all execution paths in wl filter"""
        from core.templatetags.tags import wl

        # Create mock round and teams
        team_gov = Mock()
        team_opp = Mock()
        round_mock = Mock()
        round_mock.gov = team_gov
        round_mock.opp = team_opp

        # Test all victor values for government team
        test_cases = [
            (1, "W"),  # Regular gov win
            (3, "WF"),  # Gov win by forfeit
            (6, "AW"),  # Gov win after loss
            (2, "L"),  # Opp wins, gov loses
            (4, "LF"),  # Opp wins by forfeit, gov loses by forfeit
            (5, "AL"),  # Loss after appeal
            (7, "LF"),  # Other forfeit loss
        ]

        for victor, expected in test_cases:
            round_mock.victor = victor
            result = wl(round_mock, team_gov)
            self.assertEqual(result, expected, f"Failed for victor {victor}")

        # Test opposition team wins
        round_mock.victor = 2  # Opp regular win
        result = wl(round_mock, team_opp)
        self.assertEqual(result, "W")

        round_mock.victor = 4  # Opp win by forfeit
        result = wl(round_mock, team_opp)
        self.assertEqual(result, "WF")

    def test_opponent_filter_execution(self):
        """Test opponent filter execution"""
        from core.templatetags.tags import opponent

        team_gov = Mock()
        team_opp = Mock()
        round_mock = Mock()
        round_mock.gov = team_gov
        round_mock.opp = team_opp

        # Test both directions
        result = opponent(round_mock, team_gov)
        self.assertEqual(result, team_opp)

        result = opponent(round_mock, team_opp)
        self.assertEqual(result, team_gov)

    def test_opponent_url_filter_execution(self):
        """Test opponent_url filter execution"""
        from core.templatetags.tags import opponent_url

        team_gov = Mock()
        team_opp = Mock()
        team_opp.get_absolute_url.return_value = "/team/123/"

        round_mock = Mock()
        round_mock.gov = team_gov
        round_mock.opp = team_opp

        result = opponent_url(round_mock, team_gov)
        self.assertEqual(result, "/team/123/")
        team_opp.get_absolute_url.assert_called_once()

    def test_opponent_side_filter_execution(self):
        """Test opponent_side filter execution"""
        from core.templatetags.tags import opponent_side

        team_gov = Mock()
        team_opp = Mock()
        round_mock = Mock()
        round_mock.gov = team_gov
        round_mock.opp = team_opp

        result = opponent_side(round_mock, team_gov)
        self.assertEqual(result, "OPP")

        result = opponent_side(round_mock, team_opp)
        self.assertEqual(result, "GOV")

    def test_number_filter_execution(self):
        """Test number filter execution"""
        from core.templatetags.tags import number

        # Test various number formats
        test_cases = [
            ("10.00", Decimal("10")),
            ("10.50", Decimal("10.5")),
            (15, Decimal("15")),
            ("0", Decimal("0")),
            ("100.123", Decimal("100.123")),
        ]

        for input_val, expected in test_cases:
            result = number(input_val)
            self.assertEqual(result, expected)

    def test_range_filter_execution(self):
        """Test range_filter execution"""
        from core.templatetags.tags import range_filter

        result = range_filter(1, 5)
        self.assertEqual(list(result), [1, 2, 3, 4])

        result = range_filter(0, 3)
        self.assertEqual(list(result), [0, 1, 2])

        result = range_filter(10, 12)
        self.assertEqual(list(result), [10, 11])

    def test_qual_display_filter_execution(self):
        """Test qual_display filter execution"""
        from core.templatetags.tags import qual_display

        # Mock debater with quals
        debater = Mock()
        qual1 = Mock()
        qual1.get_qual_type_display.return_value = "National"
        qual1.qual_type = 1

        qual2 = Mock()
        qual2.get_qual_type_display.return_value = "Regional"
        qual2.qual_type = 2

        qual3 = Mock()  # This one should be filtered out
        qual3.get_qual_type_display.return_value = "None"
        qual3.qual_type = 0

        quals_manager = Mock()
        quals_manager.filter.return_value.all.return_value = [qual1, qual2, qual3]
        debater.quals = quals_manager

        result = qual_display(debater, "2024")
        self.assertEqual(result, "National, Regional")

    def test_qual_contribution_filter_execution(self):
        """Test qual_contribution filter execution"""
        from core.templatetags.tags import qual_contribution

        # Test without qual
        debater = Mock()
        debater.points = 40
        debater.qualled = False

        result = qual_contribution(debater, "2024")
        self.assertEqual(result, 40)

        # Test with qual
        debater.qualled = True
        result = qual_contribution(debater, "2024")
        self.assertEqual(result, 46)  # 40 + 6

        # Test cap at 66
        debater.points = 65
        result = qual_contribution(debater, "2024")
        self.assertEqual(result, 66)  # min(65 + 6, 66)

    @patch("core.templatetags.tags.get_relevant_debaters")
    def test_relevant_debaters_filter_execution(self, mock_get_relevant):
        """Test relevant_debaters filter execution"""
        from core.templatetags.tags import relevant_debaters

        school = Mock()
        debater_list = [Mock(), Mock()]
        mock_get_relevant.return_value = debater_list

        result = relevant_debaters(school, "2024")
        self.assertEqual(result, debater_list)
        mock_get_relevant.assert_called_once_with(school, "2024")

    def test_partner_display_filter_execution(self):
        """Test partner_display filter execution"""
        from core.templatetags.tags import partner_display

        # Test with partner
        debater1 = Mock()
        debater2 = Mock()
        debater2.name = "Partner Name"
        debater2.get_absolute_url.return_value = "/debater/2/"
        debater2.school.name = "Partner School"
        debater2.school.get_absolute_url.return_value = "/school/2/"

        team = Mock()
        team.debaters.exclude.return_value.first.return_value = debater2

        result = partner_display(team, debater1)
        expected = '<a href="/debater/2/">Partner Name</a> (<a href="/school/2/">Partner School</a>)'
        self.assertEqual(result, expected)

        # Test without partner
        team.debaters.exclude.return_value.first.return_value = None
        result = partner_display(team, debater1)
        self.assertEqual(result, "NO PARTNER")

    def test_partner_name_filter_execution(self):
        """Test partner_name filter execution"""
        from core.templatetags.tags import partner_name

        # Test with partner
        debater1 = Mock()
        debater2 = Mock()
        debater2.name = "Partner Name"
        debater2.get_absolute_url.return_value = "/debater/2/"

        team = Mock()
        team.debaters.exclude.return_value.first.return_value = debater2

        result = partner_name(team, debater1)
        expected = '<a href="/debater/2/">Partner Name</a>'
        self.assertEqual(result, expected)

        # Test without partner
        team.debaters.exclude.return_value.first.return_value = None
        result = partner_name(team, debater1)
        self.assertEqual(result, "NO PARTNER")

    def test_school_filter_execution(self):
        """Test school filter execution"""
        from core.templatetags.tags import school

        team = Mock()
        school_obj = Mock()
        school_obj.name = "School Name"
        school_obj.get_absolute_url.return_value = "/school/1/"

        team.debaters.first.return_value.school = school_obj

        result = school(team)
        expected = '<a href="/school/1/">School Name</a>'
        self.assertEqual(result, expected)


@pytest.mark.django_db
def test_template_tag_registration():
    """Test template tag registration"""
    # Test that filters are registered
    assert "wl" in register.filters
    assert "opponent" in register.filters
    assert "number" in register.filters
    assert "range_filter" in register.filters
    assert "qual_display" in register.filters
    assert "qual_contribution" in register.filters
    assert "relevant_debaters" in register.filters
    assert "partner_display" in register.filters
    assert "partner_name" in register.filters
    assert "school" in register.filters


def test_all_template_filters_callable():
    """Test that all template filters are callable"""
    from core.templatetags.tags import (
        wl,
        opponent,
        opponent_url,
        opponent_side,
        number,
        range_filter,
        qual_display,
        qual_contribution,
        relevant_debaters,
        partner_display,
        partner_name,
        school,
    )

    filters = [
        wl,
        opponent,
        opponent_url,
        opponent_side,
        number,
        range_filter,
        qual_display,
        qual_contribution,
        relevant_debaters,
        partner_display,
        partner_name,
        school,
    ]

    for filter_func in filters:
        assert callable(filter_func), f"{filter_func.__name__} is not callable"
