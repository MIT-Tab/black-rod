from datetime import date, datetime, timedelta, timezone
from itertools import product
from types import SimpleNamespace

import pytest

from core.utils.immaculate_grid.boards import (
    MAXIMUM_ANSWERS, MAXIMUM_CLUE_ANSWERS, NoCompatibleBoard, build_board, cooldowns,
    answer_rarities, game_date, pair_key, scale_scores,
)
from core.utils.immaculate_grid.catalog import Catalog, Clue, rarity_score
from core.utils.immaculate_grid.rarities import rarity_tier


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
    for i, key in enumerate(keys or (*[f"clue{i}" for i in range(5)], "partner:d1")):
        clues[key] = Clue(key, key, key, people=overlap | {f"unique{i}"})
    people = set().union(*(clue.people for clue in clues.values()))
    recent = {f"person{i}" for i in range(recent_count)} | {f"unique{i}" for i in range(len(clues))}
    return Catalog({}, clues, {person: i + 10 for i, person in enumerate(sorted(people))}, recent_people=recent)


def test_board_selection_is_stable_and_scores_use_the_shared_range():
    day = date(2026, 11, 18)
    catalog = _catalog(15)
    first = build_board(day, catalog, [])
    catalog.clues = dict(reversed(catalog.clues.items()))
    second = build_board(day, catalog, [])

    assert (first.rows, first.columns, first.answers) == (second.rows, second.columns, second.answers)
    assert len(first.answers) == 9
    assert sum(clue["key"].startswith("partner:") for clue in first.rows + first.columns) == 1
    assert all(len(cell.keys() & catalog.recent_people) == 10 for cell in first.answers)
    assert all(len(cell) == 15 and 1 <= min(cell.values()) < max(cell.values()) < 100 for cell in first.answers)

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
    repeated = ("host:1:8", "host:2:8", "host:3:8", "event:Nationals:8", "SOTY:10", "school:9", "partner:d1")
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


def test_age_is_a_small_factor_for_recent_careers_and_a_large_one_for_older_eras():
    scores = [rarity_score(age, 100, 10) for age in range(17)]
    assert scores[2] - scores[0] == pytest.approx(1)
    assert scores[4] - scores[0] > 25
    assert scores[6] - scores[4] > 15
    assert scores[13] - scores[6] > 30
    assert scores == sorted(scores)


def test_longer_careers_receive_fewer_points_with_other_factors_equal():
    assert rarity_score(2, 100, 10, career_span=8) < rarity_score(2, 100, 10, career_span=4)
    assert rarity_score(2, 100, 10, career_span=4) < rarity_score(2, 100, 10, career_span=1)


def test_recent_champions_stay_less_rare_than_current_debaters_with_fewer_achievements():
    rarity = {
        "current": rarity_score(0, 100, 10, career_span=3),
        "recent_champion": rarity_score(2, 300, 20, career_span=4),
        "older_champion": rarity_score(10, 300, 20, career_span=4),
    }
    scores = scale_scores(rarity)
    assert scores["recent_champion"] < scores["current"] < scores["older_champion"]


def test_more_frequent_qualifying_results_receive_fewer_points():
    clue = Clue("partner:d1", "Partnered with One", "partner:d1")
    clue.occurrences = {"frequent": set(range(20)), "once": {1}}
    scores = scale_scores(answer_rarities({"frequent", "once"}, {"frequent": 50, "once": 50}, (clue,)))
    assert scores["frequent"] < scores["once"] == 65


def test_score_curve_spreads_middle_scores_without_granting_extra_hundreds():
    values = (0, 25, 35, 50, 65, 75, 99.9, 100)
    scores = scale_scores(dict(enumerate(values)))
    assert list(scores.values()) == [1, 30, 45, 65, 85, 95, 99, 100]


@pytest.mark.parametrize("best,second", [(19.9, 19), (30, 14.9)])
def test_generation_rejects_intersections_without_two_viable_answers(best, second):
    catalog = _catalog(15)
    catalog.rarity = dict.fromkeys(catalog.rarity, 1)
    catalog.rarity.update(person0=best, person1=second)
    with pytest.raises(NoCompatibleBoard):
        build_board(date(2026, 11, 18), catalog, [])


def test_board_lifts_low_scoring_square_to_minimums_without_lifting_easy_answers():
    catalog = _catalog(15)
    catalog.rarity = dict.fromkeys(catalog.rarity, 1)
    catalog.rarity.update(person0=20, person1=15)
    board = build_board(date(2026, 11, 18), catalog, [])
    for square in board.answers:
        assert (square["person0"], square["person1"], square["person2"]) == (35, 25, 1)


def test_four_seasons_away_offsets_attendance_and_standings_for_a_familiar_career():
    rarity = rarity_score(4, 150, 10, career_span=4, attended_tournaments=36)
    scores = scale_scores({"answer": rarity - 2, "older": 55, "common": 10})
    assert 25 <= scores["answer"] <= 35


def test_frequent_attendance_lowers_rarity_without_standings_points():
    recent_discount = rarity_score(2, 0, 0) - rarity_score(2, 0, 0, attended_tournaments=20)
    historical_discount = rarity_score(15, 0, 0) - rarity_score(15, 0, 0, attended_tournaments=20)
    assert recent_discount > 15
    assert 0 < historical_discount < recent_discount / 2


def test_partner_squares_allow_five_answers_including_three_recent():
    catalog = _catalog(15)
    catalog.clues["partner:d1"].people = {f"person{i}" for i in (0, 1, 2, 13, 14)}
    catalog.rarity.update(person13=20, person14=21)
    board = build_board(date(2026, 11, 18), catalog, [])
    assert sorted(len(cell) for cell in board.answers) == [5, 5, 5, 15, 15, 15, 15, 15, 15]
    catalog.clues["partner:d1"].people.remove("person14")
    with pytest.raises(NoCompatibleBoard):
        build_board(date(2026, 11, 18), catalog, [])
    catalog.clues["partner:d1"].people.add("person14")
    catalog.recent_people.remove("person2")
    with pytest.raises(NoCompatibleBoard):
        build_board(date(2026, 11, 18), catalog, [])


@pytest.mark.parametrize("score,key,emoji", [
    (1, "common", "🟩"), (24, "common", "🟩"), (25, "uncommon", "🟦"),
    (49, "uncommon", "🟦"), (50, "rare", "🟪"), (74, "rare", "🟪"),
    (75, "epic", "🟧"), (89, "epic", "🟧"), (90, "galaxy", "🌌"), (100, "galaxy", "🌌"),
])
def test_rarity_band_boundaries_match_share_emojis(score, key, emoji):
    assert rarity_tier(score)["key"] == key
    assert rarity_tier(score)["emoji"] == emoji
