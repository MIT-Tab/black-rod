"""Choose and publish the daily board from compatible clue intersections."""

import random
from collections import defaultdict
from datetime import timedelta, timezone as fixed_timezone
from itertools import combinations, product

from django.db import transaction
from django.utils import timezone

from core.models import ImmaculateGridBoard, SiteSetting
from core.utils.immaculate_grid.catalog import load_catalog

EST = fixed_timezone(timedelta(hours=-5), "EST")
CATEGORY_COOLDOWN = 21
PAIR_COOLDOWN = 100
MINIMUM_ANSWERS = 15
MINIMUM_RECENT_ANSWERS = 10
MAXIMUM_ANSWERS = 100
MAXIMUM_CLUE_ANSWERS = 500
GRID_WIDTH = 3
MINIMUM_CLUE_KINDS = 4
GUESS_LIMIT = GRID_WIDTH ** 2
EXTRA_GUESS_PENALTY = 5


class NoCompatibleBoard(Exception):
    pass


def game_date(now=None):
    return (now if now is not None else timezone.now()).astimezone(EST).date()


def pair_key(first, second):
    return tuple(sorted((first, second)))


def cooldowns(day, history):
    blocked_clues = set()
    blocked_pairs = set()
    for board in history:
        distance = abs((day - board.date).days)
        rows = [clue["key"] for clue in board.rows]
        columns = [clue["key"] for clue in board.columns]
        if distance <= CATEGORY_COOLDOWN:
            blocked_clues.update(rows + columns)
        if distance <= PAIR_COOLDOWN:
            blocked_pairs.update(pair_key(row, column) for row, column in product(rows, columns))
    return blocked_clues, blocked_pairs


def compatible_pairs(clues, rarity, recent_people):
    candidates = [
        clue for clue in clues.values()
        if MINIMUM_ANSWERS <= len(clue.people) <= MAXIMUM_CLUE_ANSWERS
        and len(clue.people & recent_people) >= MINIMUM_RECENT_ANSWERS
    ]
    pairs = {}
    for first, second in combinations(candidates, 2):
        if first.topic == second.topic:
            continue
        overlap = first.people & second.people
        if not MINIMUM_ANSWERS <= len(overlap) <= MAXIMUM_ANSWERS:
            continue
        if len(overlap & recent_people) < MINIMUM_RECENT_ANSWERS or overlap in (first.people, second.people):
            continue
        scores = [rarity[person] for person in overlap]
        if min(scores) < max(scores):
            pairs[pair_key(first.key, second.key)] = overlap
    return pairs


def scale_scores(people, rarity):
    minimum = min(rarity[person] for person in people)
    maximum = max(rarity[person] for person in people)
    return {
        person: round(1 + 99 * (rarity[person] - minimum) / (maximum - minimum))
        for person in sorted(people)
    }


def _column_choices(shared, order, clues, kinds, row_topics, row_kinds):
    candidates = sorted(
        (key for key in shared if clues[key].topic not in row_topics),
        key=lambda key: (kinds[key] in row_kinds, order[key]),
    )
    for columns in combinations(candidates, GRID_WIDTH):
        if len({clues[key].topic for key in columns}) != GRID_WIDTH:
            continue
        column_kinds = {kinds[key] for key in columns}
        board_kinds = column_kinds | row_kinds
        if (
            len(column_kinds) == GRID_WIDTH
            and len(board_kinds) >= MINIMUM_CLUE_KINDS
            and any(category != "placement" for category, _ in board_kinds)
        ):
            return columns
    return None


def choose_clues(day, clues, pairs, blocked_clues, blocked_pairs):
    neighbors = defaultdict(set)
    for first, second in pairs.keys() - blocked_pairs:
        if first not in blocked_clues and second not in blocked_clues:
            neighbors[first].add(second)
            neighbors[second].add(first)

    keys = sorted(key for key, adjacent in neighbors.items() if len(adjacent) >= GRID_WIDTH)
    random.Random(f"black-rod-immaculate-grid:{day.isoformat()}").shuffle(keys)
    order = {key: index for index, key in enumerate(keys)}
    kinds = {key: clues[key].kind for key in keys}

    for index, first in enumerate(keys):
        for second_index in range(index + 1, len(keys)):
            second = keys[second_index]
            first_topics = {clues[first].topic, clues[second].topic}
            first_kinds = {kinds[first], kinds[second]}
            if len(first_topics) < 2 or len(first_kinds) < 2:
                continue
            shared = neighbors[first] & neighbors[second] & order.keys()
            if len(shared) < GRID_WIDTH:
                continue
            for third in keys[second_index + 1:]:
                if clues[third].topic in first_topics or kinds[third] in first_kinds:
                    continue
                columns = _column_choices(
                    shared & neighbors[third], order, clues, kinds,
                    {*first_topics, clues[third].topic}, {*first_kinds, kinds[third]},
                )
                if columns is not None:
                    return (first, second, third), columns
    raise NoCompatibleBoard(f"No compatible Immaculate Grid board for {day}.")


def build_board(day, catalog, history):
    blocked_clues, blocked_pairs = cooldowns(day, history)
    clues = {key: clue for key, clue in catalog.clues.items() if key not in blocked_clues}
    pairs = compatible_pairs(clues, catalog.rarity, catalog.recent_people)
    rows, columns = choose_clues(day, clues, pairs, blocked_clues, blocked_pairs)
    answers = [
        scale_scores(pairs[pair_key(row, column)], catalog.rarity)
        for row, column in product(rows, columns)
    ]
    eligible = set().union(*(cell.keys() for cell in answers))
    return ImmaculateGridBoard(
        date=day,
        rows=[clues[key].display() for key in rows],
        columns=[clues[key].display() for key in columns],
        answers=answers,
        aliases={str(pk): key for pk, key in catalog.aliases.items() if key in eligible},
    )


def daily_board(day):
    existing = ImmaculateGridBoard.objects.only("date", "rows", "columns").filter(date=day).first()
    if existing is not None:
        return existing

    with transaction.atomic():
        # The upsert locks a shared row before reading the history. It also serializes
        # generation on SQLite, where SELECT FOR UPDATE has no effect.
        SiteSetting.objects.bulk_create(
            [SiteSetting(key="immaculate_grid_generation", value=day.isoformat())],
            update_conflicts=True, update_fields=["value"], unique_fields=["key"],
        )
        existing = ImmaculateGridBoard.objects.only("date", "rows", "columns").filter(date=day).first()
        if existing is not None:
            return existing
        window = timedelta(days=PAIR_COOLDOWN)
        history = ImmaculateGridBoard.objects.filter(
            date__range=(day - window, day + window)
        ).only("date", "rows", "columns")
        board = build_board(day, load_catalog(day), history)
        board.save()
        return board
