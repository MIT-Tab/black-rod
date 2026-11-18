from datetime import date, datetime, timedelta, timezone
from itertools import product
from types import SimpleNamespace

import pytest

from core.utils.immaculate_grid.boards import (
    MAXIMUM_ANSWERS, MAXIMUM_CLUE_ANSWERS, NoCompatibleBoard, build_board, cooldowns, game_date, pair_key,
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


def _catalog(overlap_size, recent_count=10, keys=None):
    overlap = {f"person{i}" for i in range(overlap_size)}
    clues = {}
    for i, key in enumerate(keys or (f"clue{i}" for i in range(6))):
        clues[key] = Clue(key, key, key, people=overlap | {f"unique{i}"})
    people = set().union(*(clue.people for clue in clues.values()))
    recent = {f"person{i}" for i in range(recent_count)} | {f"unique{i}" for i in range(len(clues))}
    return Catalog({}, clues, {person: i for i, person in enumerate(sorted(people))}, recent_people=recent)


def test_board_selection_is_stable_and_all_squares_have_equal_score_bounds():
    day = date(2026, 11, 18)
    catalog = _catalog(15)
    first = build_board(day, catalog, [])
    catalog.clues = dict(reversed(catalog.clues.items()))
    second = build_board(day, catalog, [])

    assert (first.rows, first.columns, first.answers) == (second.rows, second.columns, second.answers)
    assert len(first.answers) == 9
    assert all(len(cell.keys() & catalog.recent_people) == 10 for cell in first.answers)
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


def test_every_intersection_needs_ten_recent_debaters():
    catalog = _catalog(15, recent_count=9)
    assert all(len(clue.people & catalog.recent_people) == 10 for clue in catalog.clues.values())
    with pytest.raises(NoCompatibleBoard):
        build_board(date(2026, 11, 18), catalog, [])


def test_rejects_overly_broad_clues_and_intersections():
    day = date(2026, 11, 18)
    catalog = _catalog(15)
    catalog.clues["clue0"].people.update(f"broad{i}" for i in range(MAXIMUM_CLUE_ANSWERS))
    with pytest.raises(NoCompatibleBoard):
        build_board(day, catalog, [])
    with pytest.raises(NoCompatibleBoard):
        build_board(day, _catalog(MAXIMUM_ANSWERS + 1), [])


def test_host_and_event_quarterfinal_clues_count_as_one_kind():
    day = date(2026, 11, 18)
    repeated = ("host:1:8", "host:2:8", "host:3:8", "event:Nationals:8", "SOTY:10", "school:9")
    with pytest.raises(NoCompatibleBoard):
        build_board(day, _catalog(15, keys=repeated), [])

    catalog = _catalog(15, keys=(*repeated, "host:4:2", "speaker:5:5", "first:2024"))
    board = build_board(day, catalog, [])
    rows = {catalog.clues[clue["key"]].kind for clue in board.rows}
    columns = {catalog.clues[clue["key"]].kind for clue in board.columns}
    assert len(rows) == len(columns) == 3
    assert len(rows | columns) >= 4


def test_rarity_rises_with_older_results_fewer_points_and_less_prestige():
    reference = rarity_score(4, 20, 10)
    assert rarity_score(5, 20, 10) > reference
    assert rarity_score(4, 10, 10) > reference
    assert rarity_score(4, 20, 5) > reference
