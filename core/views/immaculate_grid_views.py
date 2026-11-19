import logging

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Case, Value, When
from django.db.models.functions import Concat, Lower, Trim
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from core.models import Debater, ImmaculateGridBoard, ImmaculateGridPlay
from core.utils.debater_aliases import load_linked_debater_ids
from core.utils.immaculate_grid.boards import (
    EXTRA_GUESS_PENALTY, GRID_WIDTH, GUESS_LIMIT, NoCompatibleBoard, clues_visible, daily_board, game_date,
)
from core.utils.immaculate_grid.catalog import person_key
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


def _board_rows(board, correct):
    return [
        {
            "label": row["label"],
            "cells": [
                {
                    "index": index,
                    "column": column["label"],
                    "answer": correct.get(index),
                }
                for index, column in enumerate(board.columns, start=row_index * GRID_WIDTH)
            ],
        }
        for row_index, row in enumerate(board.rows)
    ]


def _share_results(day, correct, attempts, score, penalty):
    squares = [
        f"{rarity_tier(correct[index]['score'])['emoji']} {correct[index]['score']}" if index in correct else "⬜ —"
        for index in range(GUESS_LIMIT)
    ]
    rows = [" | ".join(squares[start:start + GRID_WIDTH]) for start in range(0, GUESS_LIMIT, GRID_WIDTH)]
    return "\n".join([
        f"APDA Immaculate Grid · {day.isoformat()}",
        *rows,
        f"{len(correct)}/9 solved · {score} points",
        f"{attempts} guesses · {penalty} penalty points",
    ])


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
    correct = {
        item["cell"]: {**item, "tier": rarity_tier(item["score"])}
        for item in guesses if item["score"] is not None
    }
    penalty = max(0, len(guesses) - GUESS_LIMIT) * EXTRA_GUESS_PENALTY
    rarity = sum(item["score"] for item in correct.values())
    score = rarity - penalty
    return render(request, "immaculate_grid/grid.html", {
        "day": day,
        "board_id": board.pk,
        "columns": board.columns,
        "rows": _board_rows(board, correct),
        "solved": len(correct),
        "attempts": len(guesses),
        "finished": len(correct) == GUESS_LIMIT,
        "rarity": rarity,
        "penalty": penalty,
        "score": score,
        "share_results": _share_results(day, correct, len(guesses), score, penalty),
        "rarity_tiers": RARITY_TIERS,
        "latest_guess": guesses[-1] if guesses else None,
    })


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
    if play is None or len({item["cell"] for item in play.guesses if item["score"] is not None}) != GUESS_LIMIT:
        return JsonResponse({"error": "Solve all nine squares to view valid answers."}, status=403)
    if not clues_visible(board):
        return JsonResponse({"error": "This grid is unavailable."}, status=503)

    cell = form.cleaned_data["cell"]
    scores = board.answers[cell]
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
            "tier": rarity_tier(scores[person]),
        })
    options.sort(key=lambda option: (-option["score"], option["name"].casefold()))
    return JsonResponse({"answers": options})


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
