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


def score_breakdown(guesses):
    scores = [item["score"] for item in solved_guesses(guesses).values()]
    rarity = sum(scores)
    penalty = max(0, len(guesses) - GUESS_LIMIT) * EXTRA_GUESS_PENALTY
    return {
        "attempts": len(guesses),
        "rarity": rarity,
        "penalty": penalty,
        "best": max(scores, default=None),
        "worst": min(scores, default=None),
        "score": rarity - penalty,
    }


def week_start(day):
    return day - timedelta(days=day.weekday())


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
    monday = week_start(day)
    week_days = [monday + timedelta(days=offset) for offset in range(day.weekday() + 1)]
    plays = ImmaculateGridPlay.objects.filter(
        board__date__range=(monday, day), board__replaced_at__isnull=True,
    ).select_related("user", "board").only(
        "guesses", "user__username", "user__first_name", "user__last_name", "board__date",
    )
    today, week = {}, {}
    for play in plays.iterator():
        if not play.guesses:
            continue
        breakdown = score_breakdown(play.guesses)
        player = {"name": player_name(play.user), "id": play.user_id}
        total = week.setdefault(play.user_id, {**player, "score": 0, "daily_scores": {}})
        total["score"] += breakdown["score"]
        total["daily_scores"][play.board.date] = breakdown["score"]
        if play.board.date == day:
            today[play.user_id] = {**player, **breakdown}
    for entry in week.values():
        scores = entry["daily_scores"]
        entry["daily_scores"] = [(score_day, scores.get(score_day)) for score_day in week_days]
    return _rank(today, viewer_id), _rank(week, viewer_id), week_days
