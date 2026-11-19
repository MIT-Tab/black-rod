"""Choose and publish the daily board from compatible clue intersections."""

import random
from collections import defaultdict
from datetime import timedelta, timezone as fixed_timezone
from itertools import combinations, product
from math import log2

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import Debater, ImmaculateGridBoard, SiteSetting
from core.utils.immaculate_grid.catalog import hidden_people, load_catalog, person_key

EST = fixed_timezone(timedelta(hours=-5), "EST")
CATEGORY_COOLDOWN = 21
PAIR_COOLDOWN = 100
MINIMUM_ANSWERS = 15
MINIMUM_RECENT_ANSWERS = 10
PARTNER_MINIMUM_ANSWERS = 5
PARTNER_MINIMUM_RECENT_ANSWERS = 3
MAXIMUM_ANSWERS = 100
MAXIMUM_CLUE_ANSWERS = 500
GRID_WIDTH = 3
MINIMUM_CLUE_KINDS = 4
GUESS_LIMIT = GRID_WIDTH ** 2
EXTRA_GUESS_PENALTY = 5
MAXIMUM_SQUARE_BONUS = 3
POINTS_PER_BONUS_POINT = 4
REPEAT_CLUE_DISCOUNT = 2
REPEAT_PARTNER_DISCOUNT = 8


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


def _answer_minimums(*clues):
    if any(clue.key.startswith("partner:") for clue in clues):
        return PARTNER_MINIMUM_ANSWERS, PARTNER_MINIMUM_RECENT_ANSWERS
    return MINIMUM_ANSWERS, MINIMUM_RECENT_ANSWERS


def compatible_pairs(clues, rarity, recent_people):
    candidates = []
    for clue in clues.values():
        minimum, recent_minimum = _answer_minimums(clue)
        if minimum <= len(clue.people) <= MAXIMUM_CLUE_ANSWERS and len(clue.people & recent_people) >= recent_minimum:
            candidates.append(clue)
    pairs = {}
    for first, second in combinations(candidates, 2):
        if first.topic == second.topic:
            continue
        partnered = first.key.startswith("partner:") or second.key.startswith("partner:")
        minimum, recent_minimum = _answer_minimums(first, second)
        overlap = first.people & second.people
        if not minimum <= len(overlap) <= MAXIMUM_ANSWERS:
            continue
        if len(overlap & recent_people) < recent_minimum:
            continue
        if not partnered and overlap in (first.people, second.people):
            continue
        scores = answer_rarities(overlap, rarity, (first, second)).values()
        if min(scores) < max(scores):
            pairs[pair_key(first.key, second.key)] = overlap
    return pairs


def answer_rarities(people, rarity, clues):
    weighted_clues = [
        (clue, REPEAT_PARTNER_DISCOUNT if clue.kind[0] == "partner" else REPEAT_CLUE_DISCOUNT)
        for clue in clues
    ]
    return {
        person: rarity[person] - sum(weight * log2(clue.frequency(person)) for clue, weight in weighted_clues)
        for person in people
    }


def scale_scores(people, rarity, clues=()):
    scores = answer_rarities(people, rarity, clues)
    minimum = min(scores.values())
    points = {}
    for person, raw in sorted(scores.items()):
        bonus = min(MAXIMUM_SQUARE_BONUS, (raw - minimum) / POINTS_PER_BONUS_POINT)
        points[person] = min(100, max(1, round(raw + bonus)))
    return points


def _column_choices(shared, order, clues, kinds, row_topics, row_kinds):
    candidates = sorted(
        (key for key in shared if clues[key].topic not in row_topics and not key.startswith("partner:")),
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
    rng = random.Random(f"black-rod-immaculate-grid:{day.isoformat()}")
    rng.shuffle(keys)
    order = {key: index for index, key in enumerate(keys)}
    kinds = {key: clues[key].kind for key in keys}
    partners = [key for key in keys if key.startswith("partner:")]
    other_keys = [key for key in keys if not key.startswith("partner:")]

    for first in partners:
        for second_index, second in enumerate(other_keys):
            first_topics = {clues[first].topic, clues[second].topic}
            first_kinds = {kinds[first], kinds[second]}
            if len(first_topics) < 2 or len(first_kinds) < 2:
                continue
            shared = neighbors[first] & neighbors[second] & order.keys()
            if len(shared) < GRID_WIDTH:
                continue
            for third in other_keys[second_index + 1:]:
                if clues[third].topic in first_topics or kinds[third] in first_kinds:
                    continue
                columns = _column_choices(
                    shared & neighbors[third], order, clues, kinds,
                    {*first_topics, clues[third].topic}, {*first_kinds, kinds[third]},
                )
                if columns is not None:
                    if rng.getrandbits(1):
                        return columns, (first, second, third)
                    return (first, second, third), columns
    raise NoCompatibleBoard(f"No compatible Immaculate Grid board for {day}.")


def build_board(day, catalog, history):
    blocked_clues, blocked_pairs = cooldowns(day, history)
    clues = {key: clue for key, clue in catalog.clues.items() if key not in blocked_clues}
    pairs = compatible_pairs(clues, catalog.rarity, catalog.recent_people)
    rows, columns = choose_clues(day, clues, pairs, blocked_clues, blocked_pairs)
    answers = [
        scale_scores(pairs[pair_key(row, column)], catalog.rarity, (clues[row], clues[column]))
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


def clues_visible(board):
    subjects = {clue["subject"] for clue in board.rows + board.columns if "subject" in clue}
    if not subjects:
        return True
    if subjects & hidden_people():
        return False
    profile_ids = [int(person[1:]) for person in subjects if person.startswith("d")]
    group_ids = [int(person[1:]) for person in subjects if person.startswith("a")]
    public = Debater.objects.filter(Q(pk__in=profile_ids) | Q(alias_group_id__in=group_ids))
    visible = set()
    for pk, group in public.values_list("pk", "alias_group_id"):
        visible.update((f"d{pk}", person_key(pk, group)))
    return subjects <= visible


def daily_board(day):
    active = ImmaculateGridBoard.objects.only("date", "rows", "columns").filter(
        date=day, replaced_at__isnull=True,
    )
    existing = active.first()
    if existing is not None and clues_visible(existing):
        return existing

    with transaction.atomic():
        # The upsert locks a shared row before reading the history. It also serializes
        # generation on SQLite, where SELECT FOR UPDATE has no effect.
        SiteSetting.objects.bulk_create(
            [SiteSetting(key="immaculate_grid_generation", value=day.isoformat())],
            update_conflicts=True, update_fields=["value"], unique_fields=["key"],
        )
        existing = active.first()
        if existing is not None and clues_visible(existing):
            return existing
        window = timedelta(days=PAIR_COOLDOWN)
        history = ImmaculateGridBoard.objects.filter(
            date__range=(day - window, day + window)
        ).exclude(date=day).only("date", "rows", "columns")
        board = build_board(day, load_catalog(day), history)
        if not clues_visible(board):
            raise NoCompatibleBoard(f"Clue visibility changed while generating the grid for {day}.")
        if existing is not None:
            existing.replaced_at = timezone.now()
            existing.save(update_fields=["replaced_at"])
        board.save()
        return board
