import logging
from datetime import date

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Case, Value, When
from django.db.models.functions import Concat, Lower, Trim
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from core.models import Debater, ImmaculateGridBoard, ImmaculateGridPlay
from core.utils.debater_aliases import load_linked_debater_ids
from core.utils.immaculate_grid.boards import (
    GRID_WIDTH, GUESS_LIMIT, NoCompatibleBoard, clues_visible, daily_board, game_date,
)
from core.utils.immaculate_grid.catalog import load_catalog, person_key
from core.utils.immaculate_grid.leaderboard import player_name, rankings, score_breakdown, solved_guesses, week_start
from core.utils.immaculate_grid.near_misses import shortfall, threshold_for_clue
from core.utils.immaculate_grid.rarities import RARITY_TIERS, rarity_tier

logger = logging.getLogger(__name__)
GRID_URL = "core:immaculate_grid"


class GuessForm(forms.Form):
    day = forms.DateField(input_formats=["%Y-%m-%d"])
    board = forms.IntegerField(min_value=1)
    cell = forms.IntegerField(min_value=0, max_value=GUESS_LIMIT - 1)
    attempts = forms.IntegerField(min_value=0)
    debater = forms.ModelChoiceField(queryset=Debater.objects.all())


class AnswerReviewForm(forms.Form):
    day = forms.DateField(input_formats=["%Y-%m-%d"])
    board = forms.IntegerField(min_value=1)
    cell = forms.IntegerField(min_value=0, max_value=GUESS_LIMIT - 1)


def _misses_by_cell(guesses):
    misses = {}
    for guess in guesses:
        if guess["score"] is None:
            misses.setdefault(guess["cell"], []).append(guess)
    return misses


def _miss_names(misses):
    return {cell: [guess["name"] for guess in items] for cell, items in misses.items()}


def _board_rows(board, correct, misses):
    return [
        {
            "label": row["label"],
            "cells": [
                {
                    "index": index,
                    "column": column["label"],
                    "answer": correct.get(index),
                    "miss_count": len(misses.get(index, ())),
                }
                for index, column in enumerate(board.columns, start=row_index * GRID_WIDTH)
            ],
        }
        for row_index, row in enumerate(board.rows)
    ]


def _share_results(day, correct, attempts, score, penalty):
    squares = [
        f"{correct[index]['tier']['emoji']} {correct[index]['score']}" if index in correct else "⬜ —"
        for index in range(GUESS_LIMIT)
    ]
    rows = [" | ".join(squares[start:start + GRID_WIDTH]) for start in range(0, GUESS_LIMIT, GRID_WIDTH)]
    return "\n".join([
        f"APDA Immaculate Grid · {day.isoformat()}",
        *rows,
        f"{len(correct)}/9 solved · {score} points",
        f"{attempts} guesses · {penalty} penalty points",
    ])


def _correct_answers(board, guesses):
    return {
        cell: {**item, "tier": rarity_tier(item["score"], max(board.answers[cell].values(), default=None))}
        for cell, item in solved_guesses(guesses).items()
    }


@never_cache
@login_required
@require_GET
def grid(request):
    day = game_date()
    try:
        board = daily_board(day)
    except NoCompatibleBoard as exc:
        logger.warning("%s", exc)
        return render(request, "immaculate_grid/grid.html", {"day": day}, status=503)
    play = ImmaculateGridPlay.objects.filter(board=board, user=request.user).first()
    guesses = play.guesses if play else []
    correct = _correct_answers(board, guesses)
    misses = _misses_by_cell(guesses)
    breakdown = score_breakdown(guesses)
    today, week, week_days = rankings(day, request.user.pk)
    return render(request, "immaculate_grid/grid.html", {
        "day": day,
        "board_id": board.pk,
        "columns": board.columns,
        "rows": _board_rows(board, correct, misses),
        "miss_names": _miss_names(misses),
        "solved": len(correct),
        "finished": len(correct) == GUESS_LIMIT,
        **breakdown,
        "share_results": _share_results(day, correct, len(guesses), breakdown["score"], breakdown["penalty"]),
        "rarity_tiers": RARITY_TIERS,
        "latest_guess": guesses[-1] if guesses else None,
        "today_leaders": today[:3],
        "week_leaders": week[:3],
        "week_days": week_days,
        "can_view_boards": len(correct) == GUESS_LIMIT,
    })


@never_cache
@login_required
@require_GET
def leaderboard(request):
    day = game_date()
    today, week, week_days = rankings(day, request.user.pk)
    play = ImmaculateGridPlay.objects.filter(
        board__date=day, board__replaced_at__isnull=True, user=request.user,
    ).only("guesses").first()
    return render(request, "immaculate_grid/leaderboard.html", {
        "day": day, "today_leaders": today, "week_leaders": week, "week_days": week_days,
        "can_view_boards": bool(play and len(solved_guesses(play.guesses)) == GUESS_LIMIT),
    })


@never_cache
@login_required
@require_GET
def player_board(request, player_id):
    today = game_date()
    try:
        day = date.fromisoformat(request.GET.get("day", today.isoformat()))
    except ValueError as exc:
        raise Http404("This grid is unavailable.") from exc
    if not week_start(today) <= day <= today:
        raise Http404("This grid is outside the current week.")
    board = get_object_or_404(ImmaculateGridBoard, date=day, replaced_at__isnull=True)
    if not clues_visible(board):
        raise Http404("This grid is unavailable.")
    if day == today:
        viewer = ImmaculateGridPlay.objects.filter(board=board, user=request.user).only("guesses").first()
        if viewer is None or len(solved_guesses(viewer.guesses)) != GUESS_LIMIT:
            return HttpResponseForbidden("Solve all nine squares on today's board to view other players' boards.")
    play = get_object_or_404(ImmaculateGridPlay.objects.select_related("user"), board=board, user_id=player_id)
    if not play.guesses:
        return redirect("core:immaculate_grid_leaderboard")
    correct = _correct_answers(board, play.guesses)
    misses = _misses_by_cell(play.guesses)
    own_board = play.user_id == request.user.pk
    return render(request, "immaculate_grid/player_board.html", {
        "day": board.date, "player_name": player_name(play.user), "solved": len(correct),
        **score_breakdown(play.guesses), "columns": board.columns,
        "rows": _board_rows(board, correct, misses), "read_only": True,
        "board_id": board.pk, "own_board": own_board, "finished": len(correct) == GUESS_LIMIT,
        "miss_names": _miss_names(misses) if own_board else None,
    })


def _miss_review(board, guesses, cell):
    row, column = divmod(cell, GRID_WIDTH)
    clues = (board.rows[row], board.columns[column])
    memberships = board.clue_memberships
    approximate = not memberships and bool(guesses)
    thresholds = [threshold_for_clue(clue["key"]) for clue in clues]
    topics = {
        threshold[0] for clue, threshold in zip(clues, thresholds)
        if threshold and (
            approximate or any(guess["person"] not in memberships.get(clue["key"], ()) for guess in guesses)
        )
    }
    catalog = None
    if approximate or topics:
        catalog = load_catalog(board.date, progress_topics=topics, review_guesses=None if approximate else guesses)
    if approximate:
        memberships = {clue["key"]: catalog.clues[clue["key"]].people
                       for clue in clues if clue["key"] in catalog.clues}
    people = [memberships.get(clue["key"]) for clue in clues]
    misses = []
    for guess in guesses:
        person = guess["person"]
        current_person = catalog.aliases.get(guess.get("debater_id"), person) if catalog else person
        identity = current_person if approximate else person
        matches = [identity in members if members is not None else None for members in people]
        miss = {"name": guess["name"], "matches": matches}
        if topics:
            details = [
                shortfall(catalog.progress, threshold, current_person) if matched is False else None
                for matched, threshold in zip(matches, thresholds)
            ]
            if any(details):
                miss["details"] = details
        misses.append(miss)
    return {
        "misses": misses,
        "clues": [clue["label"] for clue in clues],
        "approximate": approximate,
    }


@never_cache
@login_required
@require_GET
def answers(request):
    form = AnswerReviewForm(request.GET)
    if not form.is_valid():
        return JsonResponse({"error": "Choose a grid and a square."}, status=400)
    board = ImmaculateGridBoard.objects.filter(
        date=form.cleaned_data["day"], pk=form.cleaned_data["board"], replaced_at__isnull=True,
    ).first()
    if board is None:
        return JsonResponse({"error": "This grid is unavailable."}, status=404)
    play = ImmaculateGridPlay.objects.filter(board=board, user=request.user).first()
    if play is None or len(solved_guesses(play.guesses)) != GUESS_LIMIT:
        return JsonResponse({"error": "Solve all nine squares to view valid answers."}, status=403)
    if not clues_visible(board):
        return JsonResponse({"error": "This grid is unavailable."}, status=503)

    cell = form.cleaned_data["cell"]
    scores = board.answers[cell]
    square_best = max(scores.values(), default=None)
    profile_ids = [int(pk) for pk, person in board.aliases.items() if person in scores]
    profiles = Debater.objects.filter(pk__in=profile_ids).select_related("school").order_by("pk")
    seen = set()
    options = []
    for profile in profiles:
        person = board.aliases[str(profile.pk)]
        identity = person_key(profile.pk, profile.alias_group_id)
        if identity in seen:
            continue
        seen.add(identity)
        options.append({
            "name": profile.name,
            "school": profile.school.name if profile.school else "",
            "score": scores[person],
            "tier": rarity_tier(scores[person], square_best),
        })
    options.sort(key=lambda option: (-option["score"], option["name"].casefold()))
    return JsonResponse({
        "answers": options,
        **_miss_review(board, _misses_by_cell(play.guesses).get(cell, ()), cell),
    })


@never_cache
@login_required
@require_GET
def search(request):
    query = " ".join(request.GET.get("q", "").split())[:80]
    if len(query) < 2:
        return JsonResponse({"results": []})
    candidates = Debater.objects.select_related("school").alias(
        full_name=Trim(Concat("first_name", Value(" "), "last_name"))
    )
    for word in query.split():
        candidates = candidates.filter(full_name__icontains=word)
    candidates = candidates.order_by(
        Case(
            When(full_name__iexact=query, then=Value(0)),
            When(full_name__istartswith=query, then=Value(1)),
            default=Value(2),
        ),
        Lower("full_name"), "pk",
    )
    results = []
    seen = set()
    for debater in candidates.iterator(chunk_size=32):
        person = person_key(debater.pk, debater.alias_group_id)
        if person in seen:
            continue
        seen.add(person)
        results.append({
            "id": debater.pk, "person": person, "name": debater.name,
            "school": debater.school.name if debater.school else "",
        })
        if len(results) == 12:
            break
    return JsonResponse({"results": results})


def _guess_issue(play, attempts, cell, people, profile_ids):
    if len(play.guesses) != attempts:
        return "Your grid was updated. Choose a square to continue."
    for item in play.guesses:
        if item["cell"] == cell and item["score"] is not None:
            return "That square is already solved."
        same_person = item["person"] in people or item.get("debater_id") in profile_ids
        if same_person and (item["score"] is not None or item["cell"] == cell):
            return "You’ve already used that debater."
    return None


@never_cache
@login_required
@require_POST
def guess(request):
    form = GuessForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose a square and select a debater from the search results.")
        return redirect(GRID_URL)

    day = game_date()
    data = form.cleaned_data
    if data["day"] != day:
        messages.info(request, "A new daily grid is ready.")
        return redirect(GRID_URL)
    board = ImmaculateGridBoard.objects.filter(
        date=day, pk=data["board"], replaced_at__isnull=True,
    ).first()
    if board is None or not clues_visible(board):
        messages.info(request, "This grid has changed. Choose a square on the latest board.")
        return redirect(GRID_URL)
    play, _ = ImmaculateGridPlay.objects.get_or_create(board=board, user=request.user)
    debater = data["debater"]
    person = board.aliases.get(str(debater.pk)) or person_key(debater.pk, debater.alias_group_id)
    profile_ids = load_linked_debater_ids([debater.pk]) if debater.alias_group_id else {debater.pk}
    people = {f"d{pk}" for pk in profile_ids} | {person_key(debater.pk, debater.alias_group_id)}
    people.update(board.aliases[str(pk)] for pk in profile_ids if str(pk) in board.aliases)
    cell = data["cell"]
    issue = _guess_issue(play, data["attempts"], cell, people, profile_ids)
    if issue:
        messages.info(request, issue)
        return redirect(GRID_URL)
    if day != game_date():
        messages.info(request, "A new daily grid is ready.")
        return redirect(GRID_URL)

    item = {
        "cell": cell, "person": person, "debater_id": debater.pk,
        "name": debater.name, "score": board.answers[cell].get(person),
    }
    if not ImmaculateGridPlay.objects.filter(pk=play.pk, guesses=play.guesses).update(
        guesses=[*play.guesses, item]
    ):
        messages.info(request, "Your grid was updated. Choose a square to continue.")
    return redirect(GRID_URL)
