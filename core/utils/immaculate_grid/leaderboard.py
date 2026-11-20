"""Display scores and ranks for the current game day and week."""

from datetime import timedelta

from core.models import ImmaculateGridPlay
from core.utils.immaculate_grid.boards import EXTRA_GUESS_PENALTY, GUESS_LIMIT


def player_name(user):
    name = user.get_full_name().strip()
    if name:
        return name
    return user.get_username() if "@" not in user.get_username() else f"Player {user.pk}"


def solved_guesses(guesses):
    return {item["cell"]: item for item in guesses if item["score"] is not None}


def score_guesses(guesses):
    rarity = sum(item["score"] for item in solved_guesses(guesses).values())
    penalty = max(0, len(guesses) - GUESS_LIMIT) * EXTRA_GUESS_PENALTY
    return rarity, penalty


def _rank(entries, viewer_id):
    ordered = sorted(entries.values(), key=lambda row: (-row["score"], row["name"].casefold(), row["id"]))
    previous_score = None
    rank = 0
    for index, row in enumerate(ordered, 1):
        if previous_score != row["score"]:
            rank = index
            previous_score = row["score"]
        row.update(rank=rank, is_viewer=row["id"] == viewer_id)
    return ordered


def rankings(day, viewer_id):
    week_start = day - timedelta(days=day.weekday())
    plays = ImmaculateGridPlay.objects.filter(
        board__date__range=(week_start, day), board__replaced_at__isnull=True,
    ).select_related("user", "board").only(
        "guesses", "user__username", "user__first_name", "user__last_name", "board__date",
    )
    today, week = {}, {}
    for play in plays.iterator():
        if not play.guesses:
            continue
        rarity, penalty = score_guesses(play.guesses)
        row = {"name": player_name(play.user), "id": play.user_id, "score": rarity - penalty}
        total = week.setdefault(play.user_id, {**row, "score": 0})
        total["score"] += row["score"]
        if play.board.date == day:
            today[play.user_id] = row
    return _rank(today, viewer_id), _rank(week, viewer_id)
