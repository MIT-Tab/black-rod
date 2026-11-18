import logging

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from core.models import Debater, ImmaculateGridBoard, ImmaculateGridPlay
from core.utils.immaculate_grid.boards import (
    GRID_WIDTH, GUESS_LIMIT, NoCompatibleBoard, daily_board, game_date,
)
from core.utils.immaculate_grid.catalog import hidden_people, person_key

logger = logging.getLogger(__name__)
GRID_URL = "core:immaculate_grid"


class GuessForm(forms.Form):
    day = forms.DateField(input_formats=["%Y-%m-%d"])
    cell = forms.IntegerField(min_value=0, max_value=GUESS_LIMIT - 1)
    attempts = forms.IntegerField(min_value=0, max_value=GUESS_LIMIT - 1)
    debater = forms.ModelChoiceField(queryset=Debater.objects.all())


def _clues_visible(board):
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
    if not _clues_visible(board):
        return render(request, "immaculate_grid/grid.html", {"day": day}, status=503)

    play = ImmaculateGridPlay.objects.filter(board=board, user=request.user).first()
    guesses = play.guesses if play else []
    correct = {item["cell"]: item for item in guesses if item["score"] is not None}
    return render(request, "immaculate_grid/grid.html", {
        "day": day,
        "columns": board.columns,
        "rows": _board_rows(board, correct),
        "solved": len(correct),
        "attempts": len(guesses),
        "remaining": GUESS_LIMIT - len(guesses),
        "rarity": sum(item["score"] for item in correct.values()),
        "latest_guess": guesses[-1] if guesses else None,
    })


@never_cache
@login_required
@require_GET
def search(request):
    query = request.GET.get("q", "").strip()[:80]
    if len(query) < 2:
        return JsonResponse({"results": []})
    candidates = Debater.objects.select_related("school")
    for word in query.split():
        candidates = candidates.filter(Q(first_name__icontains=word) | Q(last_name__icontains=word))
    candidates = candidates.order_by("first_name", "last_name", "pk")[:12]
    return JsonResponse({"results": [
        {"id": debater.pk, "name": debater.name, "school": debater.school.name if debater.school else ""}
        for debater in candidates
    ]})


def _guess_issue(play, attempts, cell, person):
    if len(play.guesses) >= GUESS_LIMIT:
        return "You’ve used all nine guesses today."
    if len(play.guesses) != attempts:
        return "Your grid was updated. Choose a square to continue."
    for item in play.guesses:
        if item["cell"] == cell and item["score"] is not None:
            return "That square is already solved."
        if item["person"] == person and (item["score"] is not None or item["cell"] == cell):
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
    board = ImmaculateGridBoard.objects.filter(date=day).first()
    if board is None or not _clues_visible(board):
        return redirect(GRID_URL)
    play, _ = ImmaculateGridPlay.objects.get_or_create(board=board, user=request.user)
    debater = data["debater"]
    person = board.aliases.get(str(debater.pk)) or person_key(debater.pk, debater.alias_group_id)
    cell = data["cell"]
    issue = _guess_issue(play, data["attempts"], cell, person)
    if issue:
        messages.info(request, issue)
        return redirect(GRID_URL)
    if day != game_date():
        messages.info(request, "A new daily grid is ready.")
        return redirect(GRID_URL)

    item = {"cell": cell, "person": person, "name": debater.name, "score": board.answers[cell].get(person)}
    if not ImmaculateGridPlay.objects.filter(pk=play.pk, guesses=play.guesses).update(
        guesses=[*play.guesses, item]
    ):
        messages.info(request, "Your grid was updated. Choose a square to continue.")
    return redirect(GRID_URL)
