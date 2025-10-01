"""Boundary coverage for core.utils.points."""

import math

import pytest

from core.utils import points


@pytest.mark.parametrize(
    "place,expected",
    [
        (1, 12.5),
        (2, 10),
        (3, 7.5),
        (4, 7.5),
        (5, 5),
        (8, 5),
        (9, 2.5),
        (16, 2.5),
        (17, 1.25),
        (32, 1.25),
        (33, 0),
    ],
)
def test_online_points_piecewise_breakpoints(place, expected):
    assert points.online_points(place) == expected


@pytest.mark.parametrize(
    "num_teams,place,ghost,expected",
    [
        (6, 1, False, 0),
        (10, 1, False, 8),
        (10, 2, False, 4),
        (10, 3, False, 0),
        (24, 1, False, 13),
        (24, 2, False, 9),
        (24, 3, False, 3.75),
        (24, 5, False, 0.5),
        (24, 5, True, 3.75),
        (24, 9, True, math.floor((24 - 16) / 8) * 0.5),
        (75, 1, False, 19),
        (75, 5, False, 3.5),
        (75, 10, False, 0.75),
        (80, 1, False, 20),
        (80, 2, False, 16),
        (80, 5, False, 4),
        (80, 6, False, 4),
        (80, 10, False, 1.5),
    ],
)
def test_team_points_respects_size_bands(num_teams, place, ghost, expected):
    assert points.team_points_for_size(num_teams, place, ghost) == pytest.approx(expected)


@pytest.mark.parametrize(
    "num_teams,place,expected",
    [
        (6, 1, 0),
        (10, 1, 8),
        (10, 3, 3),
        (24, 1, 13),
        (24, 2, 10.5),
        (80, 1, 20),
        (80, 5, max(0, 20 - 2.5 * 4)),
    ],
)
def test_speaker_points_decay_by_two_point_five(num_teams, place, expected):
    assert points.speaker_points_for_size(num_teams, place) == pytest.approx(expected)


@pytest.mark.parametrize(
    "num_novices,place,expected",
    [
        (5, 1, 10),
        (5, 2, 7.5),
        (20, 1, 12),
        (60, 1, 17),
        (60, 4, 9.5),
    ],
)
def test_novice_points_cap_and_decay(num_novices, place, expected):
    assert points.novice_points_for_size(num_novices, place) == pytest.approx(expected)


def test_extreme_places_drop_to_zero():
    assert points.team_points_for_size(40, 99) == 0
    assert points.speaker_points_for_size(40, 99) == 0
