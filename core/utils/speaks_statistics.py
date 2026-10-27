"""Seasonal distributions of complete preliminary speaker-score totals."""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Avg

from core.models import Round, RoundStats, SpeakerResult, Team, TeamResult, Tournament


METRICS = (
    ("breaking_teams", "Breaking teams combined speaks"),
    ("novice_breaking_teams", "Novice breaking teams combined speaks"),
    ("top_breaking_seed", "Top breaking seed combined speaks"),
    ("bottom_breaking_seed", "Bottom breaking seed combined speaks"),
    ("top_full_field_seed", "Top full-field seed combined speaks"),
    ("bottom_full_field_seed", "Bottom full-field seed combined speaks"),
    ("first_varsity_speaker", "1st varsity speaker speaks"),
    ("tenth_varsity_speaker", "10th varsity speaker speaks"),
    ("first_novice_speaker", "1st novice speaker speaks"),
)


def summarize_sample(values):
    """Use linear interpolation at (n - 1) * p, including singleton samples."""
    ordered = sorted(values)
    summary = {"sample_size": len(ordered)}
    for percentile in (25, 50, 75):
        value = None
        if ordered:
            position = Decimal(len(ordered) - 1) * Decimal(percentile) / 100
            lower = int(position)
            upper = min(lower + 1, len(ordered) - 1)
            value = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
        summary[f"p{percentile}"] = value
    return summary


def get_speaks_statistics():
    """Return newest seasons first, omitting seasons without usable samples.

    Load one season at a time with a fixed number of queries per season, rather
    than querying each tournament, team, or speaker separately.
    """
    by_season = defaultdict(list)
    for tournament in Tournament.objects.order_by("-season", "id"):
        by_season[tournament.season].append(tournament)

    seasons = []
    for season, tournaments in by_season.items():
        samples = _season_samples(tournaments)
        if not any(samples.values()):
            continue
        seasons.append({
            "season": season,
            "season_display": tournaments[0].get_season_display(),
            "metrics": [
                {"key": key, "label": label, **summarize_sample(samples[key])}
                for key, label in METRICS
            ],
        })
    return seasons


def _season_samples(tournaments):
    tournament_ids = [tournament.pk for tournament in tournaments]
    rounds = defaultdict(list)
    team_ids = set()
    for round_obj in Round.objects.filter(tournament_id__in=tournament_ids).only(
        "tournament_id", "gov_id", "opp_id", "round_number", "stage", "division", "victor",
    ).order_by():
        rounds[round_obj.tournament_id].append(round_obj)
        team_ids.update((round_obj.gov_id, round_obj.opp_id))

    members = defaultdict(set)
    for team_id, debater_id in Team.debaters.through.objects.filter(
        team_id__in=team_ids,
    ).values_list("team_id", "debater_id"):
        members[team_id].add(debater_id)

    scores = defaultdict(dict)
    # Panels contribute one average per debater per round, not multiple speeches.
    for row in RoundStats.objects.filter(
        round__tournament_id__in=tournament_ids,
        round__stage=Round.Stage.PRELIM,
    ).order_by().values("round_id", "debater_id").annotate(
        speaks=Avg("speaks"), ranks=Avg("ranks"),
    ):
        scores[row["round_id"]][row["debater_id"]] = (row["speaks"], row["ranks"])

    team_results = defaultdict(list)
    for row in TeamResult.objects.filter(
        tournament_id__in=tournament_ids, place__gt=0,
    ).values("tournament_id", "team_id", "type_of_place"):
        team_results[row["tournament_id"]].append(row)

    speaker_results = defaultdict(list)
    for row in SpeakerResult.objects.filter(
        tournament_id__in=tournament_ids, place__in=(1, 2, 10, 11),
    ).values("tournament_id", "debater_id", "type_of_place", "place", "tie"):
        speaker_results[row["tournament_id"]].append(row)

    samples = defaultdict(list)
    for tournament in tournaments:
        _add_tournament_samples(
            samples, tournament, rounds[tournament.pk], scores, members,
            team_results[tournament.pk], speaker_results[tournament.pk],
        )
    return samples


def _complete_total(entries, expected_rounds):
    """Reject incomplete cards and duplicate round numbers instead of filling zeros."""
    if expected_rounds <= 0 or len(entries) != expected_rounds:
        return None
    if {number for number, _ in entries} != set(range(1, expected_rounds + 1)):
        return None
    if any(value is None or value <= 0 for _, value in entries):
        return None
    return sum(value for _, value in entries)


def _add_tournament_samples(samples, tournament, rounds, scores, members,
                            team_results, speaker_results):
    breaks = {TeamResult.VARSITY: set(), TeamResult.NOVICE: set()}
    for result in team_results:
        breaks[result["type_of_place"]].add(result["team_id"])

    team_cards = defaultdict(list)
    rank_cards = defaultdict(list)
    speaker_cards = defaultdict(list)
    wins = defaultdict(int)
    unknown_outcomes = set()
    for round_obj in rounds:
        if (round_obj.stage == Round.Stage.OUTROUND
                or round_obj.round_number > tournament.num_rounds):
            division = (TeamResult.NOVICE if round_obj.division == Round.Division.NOVICE
                        else TeamResult.VARSITY)
            breaks[division].update((round_obj.gov_id, round_obj.opp_id))
            continue
        if round_obj.round_number < 1:
            continue
        round_scores = scores[round_obj.pk]
        for debater_id, (speaks, _) in round_scores.items():
            speaker_cards[debater_id].append((round_obj.round_number, speaks))
        for team_id, winning_outcomes in (
            (round_obj.gov_id, (Round.GOV, Round.GOV_VIA_FORFEIT, Round.ALL_WIN, Round.BYE)),
            (round_obj.opp_id, (Round.OPP, Round.OPP_VIA_FORFEIT, Round.ALL_WIN, Round.BYE)),
        ):
            team_scores = [round_scores.get(member, (None, None)) for member in members[team_id]]
            complete = len(team_scores) == 2 and all(
                score[0] is not None and score[0] > 0 for score in team_scores
            )
            speaks = sum(score[0] for score in team_scores) if complete else None
            ranks = (sum(score[1] for score in team_scores)
                     if complete and all(score[1] is not None for score in team_scores) else None)
            team_cards[team_id].append((round_obj.round_number, speaks))
            rank_cards[team_id].append((round_obj.round_number, ranks))
            wins[team_id] += round_obj.victor in winning_outcomes
            if round_obj.victor == Round.UNKNOWN:
                unknown_outcomes.add(team_id)

    totals = {team_id: _complete_total(card, tournament.num_rounds)
              for team_id, card in team_cards.items()}
    ranks = {team_id: _complete_total(card, tournament.num_rounds)
             for team_id, card in rank_cards.items()}
    for division, key in ((TeamResult.VARSITY, "breaking_teams"),
                          (TeamResult.NOVICE, "novice_breaking_teams")):
        samples[key].extend(totals[team_id] for team_id in breaks[division]
                            if totals.get(team_id) is not None)

    _add_seed_samples(samples, "breaking", breaks[TeamResult.VARSITY], totals,
                      ranks, wins, unknown_outcomes)
    if tournament.num_teams <= 0 or len(team_cards) == tournament.num_teams:
        _add_seed_samples(samples, "full_field", set(team_cards), totals,
                          ranks, wins, unknown_outcomes)

    speaker_metrics = {
        (SpeakerResult.VARSITY, 1): "first_varsity_speaker",
        (SpeakerResult.VARSITY, 10): "tenth_varsity_speaker",
        (SpeakerResult.NOVICE, 1): "first_novice_speaker",
    }
    seen = set()
    for result in speaker_results:
        # The results page displays tied speakers one place above their stored slot.
        place = result["place"] - int(result["tie"])
        key = speaker_metrics.get((result["type_of_place"], place))
        identity = (key, result["debater_id"])
        if key is None or identity in seen:
            continue
        seen.add(identity)
        total = _complete_total(speaker_cards[result["debater_id"]], tournament.num_rounds)
        if total is not None:
            samples[key].append(total)


def _add_seed_samples(samples, group, candidates, totals, ranks, wins, unknown_outcomes):
    # Missing competitors could change which seed is first or last. Do not
    # silently select the best/worst of just the subset with complete scores.
    if (not candidates or candidates & unknown_outcomes
            or any(totals.get(team_id) is None for team_id in candidates)):
        return
    use_ranks = all(ranks.get(team_id) is not None for team_id in candidates)
    keys = {team_id: (wins[team_id], totals[team_id], -ranks[team_id] if use_ranks else 0)
            for team_id in candidates}
    for label, target in (("top", max(keys.values())), ("bottom", min(keys.values()))):
        samples[f"{label}_{group}_seed"].extend(
            totals[team_id] for team_id, key in keys.items() if key == target
        )
