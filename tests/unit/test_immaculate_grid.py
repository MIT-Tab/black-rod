from datetime import date, datetime, timedelta, timezone
from itertools import product
from types import SimpleNamespace

import pytest

from core.utils.immaculate_grid.boards import (
    NoCompatibleBoard, build_board, cooldowns, game_date, pair_key,
)
from core.utils.immaculate_grid.catalog import Catalog, Clue, rarity_score


def test_day_changes_at_midnight_standard_time_even_in_summer():
    before = datetime(2026, 7, 9, 4, 59, tzinfo=timezone.utc)
    assert game_date(before) == date(2026, 7, 8)
    assert game_date(before + timedelta(minutes=1)) == date(2026, 7, 9)


def test_cooldowns_include_last_21_and_100_days_and_ignore_axis_order():
    day = date(2026, 11, 18)
    history = [
        SimpleNamespace(date=day - timedelta(days=ago), rows=[{"key": row}], columns=[{"key": col}])
        for ago, row, col in [(21, "school", "partner"), (100, "season", "host"), (101, "soty", "toty")]
    ]
    clues, pairs = cooldowns(day, history)
    assert clues == {"school", "partner"}
    assert pairs == {pair_key("partner", "school"), pair_key("host", "season")}


def _catalog(overlap_size):
    overlap = {f"person{i}" for i in range(overlap_size)}
    clues = {}
    for i in range(6):
        key = f"clue{i}"
        clues[key] = Clue(key, key, key, people=overlap | {f"unique{i}"})
    people = set().union(*(clue.people for clue in clues.values()))
    return Catalog({}, clues, {person: i for i, person in enumerate(sorted(people))})


def test_board_selection_is_stable_and_all_squares_have_equal_score_bounds():
    day = date(2026, 11, 18)
    catalog = _catalog(15)
    first = build_board(day, catalog, [])
    catalog.clues = dict(reversed(catalog.clues.items()))
    second = build_board(day, catalog, [])

    assert (first.rows, first.columns, first.answers) == (second.rows, second.columns, second.answers)
    assert len(first.answers) == 9
    assert all(len(cell) == 15 and min(cell.values()) == 1 and max(cell.values()) == 100 for cell in first.answers)

    later = day + timedelta(days=100)
    with pytest.raises(NoCompatibleBoard):
        build_board(later, catalog, [first])
    after_cooldown = build_board(later + timedelta(days=1), catalog, [first])
    assert len(list(product(after_cooldown.rows, after_cooldown.columns))) == 9


def test_board_selection_does_not_publish_undersized_or_unscorable_cells():
    day = date(2026, 11, 18)
    with pytest.raises(NoCompatibleBoard):
        build_board(day, _catalog(14), [])

    tied = _catalog(15)
    tied.rarity = dict.fromkeys(tied.rarity, 50)
    with pytest.raises(NoCompatibleBoard):
        build_board(day, tied, [])


def test_rarity_rises_with_older_results_fewer_points_and_less_prestige():
    reference = rarity_score(4, 20, 10)
    assert rarity_score(5, 20, 10) > reference
    assert rarity_score(4, 10, 10) > reference
    assert rarity_score(4, 20, 5) > reference
