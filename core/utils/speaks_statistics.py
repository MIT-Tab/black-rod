"""Seasonal distributions of complete preliminary speaker-score totals."""

from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, Sum

from core.models import Debater, Round, RoundStats, SpeakerResult, Team, TeamResult, Tournament
from core.utils.debater_aliases import build_representative_debater_maps
from core.utils.elo_runtime_engine.debate_contract import (
    metadata_team_ids,
    stage_for_rating,
    winner_code_for_debate,
)
from core.utils.speaks_distributions import (
    add_round_speaks_samples,
    new_round_speaks_samples,
    summarize_round_speaks,
    summarize_sample,
)


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


def get_speaks_statistics():
    """Return newest seasons first, omitting seasons without usable samples.

    Load one season at a time with a fixed number of queries per season, rather
    than querying each tournament, team, or speaker separately.
    """
    by_season = defaultdict(list)
    for tournament in Tournament.objects.order_by("-season", "id"):
        by_season[tournament.season].append(tournament)

    identities = list(Debater.all_objects.values_list("id", "alias_group_id", "first_season"))
    representatives, _ = build_representative_debater_maps(
        (debater_id, alias_id) for debater_id, alias_id, _ in identities
    )
    first_seasons = {}
    for debater_id, _, first_season in identities:
        if first_season:
            identity = representatives[debater_id]
            first_seasons[identity] = min(first_season, first_seasons.get(identity, first_season))

    seasons = []
    for season, tournaments in by_season.items():
        samples, coverage, scored_tournaments, round_samples = _season_samples(
            tournaments, representatives, first_seasons,
        )
        if not any(samples.values()) and not round_samples["scores"]:
            continue
        seasons.append({
            "season": season,
            "season_display": tournaments[0].get_season_display(),
            "scored_tournaments": scored_tournaments,
            "chart_id": f"speaks-cdf-{len(seasons)}",
            "round_speaks": summarize_round_speaks(round_samples),
            "metrics": [
                {"key": key, "label": label, "tournament_count": coverage[key],
                 **summarize_sample(samples[key])}
                for key, label in METRICS
            ],
        })
    return seasons


def _season_samples(tournaments, representatives, first_seasons):
    tournament_ids = [tournament.pk for tournament in tournaments]
    team_results = defaultdict(list)
    team_ids = set()
    for row in TeamResult.objects.filter(
        tournament_id__in=tournament_ids, place__gt=0,
    ).values("tournament_id", "team_id", "type_of_place"):
        team_results[row["tournament_id"]].append(row)
        team_ids.add(row["team_id"])

    rounds = defaultdict(list)
    for round_obj in Round.objects.filter(tournament_id__in=tournament_ids).select_related(
        "imported_metadata__gov_1_alias", "imported_metadata__gov_2_alias",
        "imported_metadata__opp_1_alias", "imported_metadata__opp_2_alias",
    ).order_by():
        rounds[round_obj.tournament_id].append(round_obj)
        team_ids.update((round_obj.gov_id, round_obj.opp_id))

    members = defaultdict(set)
    for team_id, debater_id in Team.debaters.through.objects.filter(
        team_id__in=team_ids,
    ).values_list("team_id", "debater_id"):
        members[team_id].add(representatives[debater_id])

    scores = defaultdict(dict)
    # Resolve linked profiles before averaging panels, preserving ballot weights.
    for row in RoundStats.objects.filter(
        round__tournament_id__in=tournament_ids,
        round__stage=Round.Stage.PRELIM,
    ).order_by().values("round_id", "debater_id").annotate(
        speaks_sum=Sum("speaks"), speaks_count=Count("speaks"),
        ranks_sum=Sum("ranks"), ranks_count=Count("ranks"),
    ):
        identity = representatives[row["debater_id"]]
        aggregate = scores[row["round_id"]].setdefault(identity, [Decimal(0), 0, Decimal(0), 0])
        aggregate[0] += row["speaks_sum"] or 0
        aggregate[1] += row["speaks_count"]
        aggregate[2] += row["ranks_sum"] or 0
        aggregate[3] += row["ranks_count"]
    scores = {
        round_id: {identity: (values[0] / values[1] if values[1] else None,
                              values[2] / values[3] if values[3] else None)
                   for identity, values in rows.items()}
        for round_id, rows in scores.items()
    }

    speaker_results = defaultdict(list)
    for row in SpeakerResult.objects.filter(
        tournament_id__in=tournament_ids, place__gt=0,
    ).values("tournament_id", "debater_id", "type_of_place", "place", "tie"):
        row["debater_id"] = representatives[row["debater_id"]]
        speaker_results[row["tournament_id"]].append(row)

    samples = defaultdict(list)
    coverage = defaultdict(int)
    round_samples = new_round_speaks_samples()
    scored_tournaments = 0
    for tournament in tournaments:
        previous_sizes = {key: len(samples[key]) for key, _ in METRICS}
        novices = {identity for identity, season in first_seasons.items() if season == tournament.season}
        scored_tournaments += _add_tournament_samples(
            samples, tournament, rounds[tournament.pk], scores, members,
            team_results[tournament.pk], speaker_results[tournament.pk], representatives, novices,
            round_samples,
        )
        for key, _ in METRICS:
            coverage[key] += len(samples[key]) > previous_sizes[key]
    return samples, coverage, scored_tournaments, round_samples


def _complete_round_numbers(entries, expected_rounds):
    return (expected_rounds > 0 and len(entries) == expected_rounds
            and {number for number, _ in entries} == set(range(1, expected_rounds + 1)))


def _complete_total(entries, expected_rounds):
    """Reject incomplete cards and duplicate round numbers instead of filling zeros."""
    if not _complete_round_numbers(entries, expected_rounds):
        return None
    if any(value is None or value <= 0 for _, value in entries):
        return None
    return sum(value for _, value in entries)


def _partnership(debater_ids):
    identity = tuple(sorted(set(debater_ids)))
    return identity if len(identity) == 2 else None


def _round_partnership(round_obj, side, members, representatives):
    try:
        metadata = round_obj.imported_metadata
    except ObjectDoesNotExist:
        metadata = None
    if metadata:
        aliases = (getattr(metadata, f"{side}_1_alias"), getattr(metadata, f"{side}_2_alias"))
        if all(aliases):
            return _partnership(representatives[alias.debater_id] for alias in aliases)
    field = "team_a_ids" if side == "gov" else "team_b_ids"
    raw_metadata = round_obj.metadata if isinstance(round_obj.metadata, dict) else {}
    identity_ids = metadata_team_ids(raw_metadata, field)
    if len(identity_ids) == 2:
        return _partnership(representatives.get(value, value) for value in identity_ids)
    return _partnership(members[getattr(round_obj, f"{side}_id")])


def _add_tournament_samples(samples, tournament, rounds, scores, members,
                            team_results, speaker_results, representatives, novices, round_samples):
    breaks = {TeamResult.VARSITY: set(), TeamResult.NOVICE: set()}
    for result in team_results:
        identity = _partnership(members[result["team_id"]])
        if identity:
            breaks[result["type_of_place"]].add(identity)
    novices.update(result["debater_id"] for result in speaker_results
                   if result["type_of_place"] == SpeakerResult.NOVICE)

    team_cards = defaultdict(list)
    rank_cards = defaultdict(list)
    speaker_cards = defaultdict(list)
    speaker_rank_cards = defaultdict(list)
    wins = defaultdict(int)
    unknown_outcomes = set()
    observations = []
    for round_obj in rounds:
        gov = _round_partnership(round_obj, "gov", members, representatives)
        opp = _round_partnership(round_obj, "opp", members, representatives)
        if (stage_for_rating(round_obj) == Round.Stage.OUTROUND
                or round_obj.round_number > tournament.num_rounds):
            division = (TeamResult.NOVICE if (round_obj.division == Round.Division.NOVICE
                                             or tournament.qual_type == Tournament.NOVICE)
                        else TeamResult.VARSITY)
            breaks[division].update(identity for identity in (gov, opp) if identity)
            continue
        if round_obj.round_number < 1:
            continue
        round_scores = scores.get(round_obj.pk, {})
        for debater_id, (speaks, ranks) in round_scores.items():
            speaker_cards[debater_id].append((round_obj.round_number, speaks))
            speaker_rank_cards[debater_id].append((round_obj.round_number, ranks))
            if speaks is not None and speaks > 0:
                owners = {team for team in (gov, opp) if team and debater_id in team}
                owner = next(iter(owners)) if len(owners) == 1 else None
                observations.append((round_obj.round_number, debater_id, speaks, owner))
        winner = winner_code_for_debate(round_obj)
        for team_id, side in (
            (gov, "a"),
            (opp, "b"),
        ):
            if team_id is None:
                continue
            team_scores = [round_scores.get(member, (None, None)) for member in team_id]
            complete = len(team_scores) == 2 and all(
                score[0] is not None and score[0] > 0 for score in team_scores
            )
            speaks = sum(score[0] for score in team_scores) if complete else None
            ranks = (sum(score[1] for score in team_scores)
                     if complete and all(score[1] is not None for score in team_scores) else None)
            team_cards[team_id].append((round_obj.round_number, speaks))
            rank_cards[team_id].append((round_obj.round_number, ranks))
            wins[team_id] += (winner == side if winner else
                              round_obj.victor in (Round.ALL_WIN, Round.BYE))
            if not winner and round_obj.victor not in (Round.ALL_DROP, Round.ALL_WIN, Round.BYE):
                unknown_outcomes.add(team_id)

    speaker_totals = {identity: _complete_total(card, tournament.num_rounds)
                      for identity, card in speaker_cards.items()}
    candidates = set(team_cards) | breaks[TeamResult.VARSITY] | breaks[TeamResult.NOVICE]
    totals = {team: sum(speaker_totals[member] for member in team)
              if all(speaker_totals.get(member) is not None for member in team) else None
              for team in candidates}
    ranks = {team_id: _complete_total(card, tournament.num_rounds)
             for team_id, card in rank_cards.items()}
    unknown_outcomes.update(team for team in candidates
                            if not _complete_round_numbers(team_cards[team], tournament.num_rounds))
    add_round_speaks_samples(round_samples, tournament, observations, speaker_cards, wins, unknown_outcomes)
    breaks[TeamResult.NOVICE].update(team for team in breaks[TeamResult.VARSITY]
                                    if set(team) <= novices)
    for division, key in ((TeamResult.VARSITY, "breaking_teams"),
                          (TeamResult.NOVICE, "novice_breaking_teams")):
        samples[key].extend(totals[team_id] for team_id in breaks[division]
                            if totals.get(team_id) is not None)

    _add_seed_samples(samples, "breaking", breaks[TeamResult.VARSITY], totals,
                      ranks, wins, unknown_outcomes)
    _add_seed_samples(samples, "full_field", set(team_cards), totals,
                      ranks, wins, unknown_outcomes)

    speaker_ranks = {identity: _complete_total(card, tournament.num_rounds)
                     for identity, card in speaker_rank_cards.items()}
    _add_speaker_samples(samples, speaker_results, speaker_totals, speaker_ranks, novices)
    return any(value is not None and value > 0 for card in speaker_cards.values() for _, value in card)


def _add_speaker_samples(samples, speaker_results, totals, ranks, novices):
    speaker_metrics = {
        (SpeakerResult.VARSITY, 1): "first_varsity_speaker",
        (SpeakerResult.VARSITY, 10): "tenth_varsity_speaker",
        (SpeakerResult.NOVICE, 1): "first_novice_speaker",
    }
    for (division, place), key in speaker_metrics.items():
        placed = {result["debater_id"] for result in speaker_results
                  if result["type_of_place"] == division
                  and max(1, result["place"] - int(result["tie"])) == place}
        if placed:
            samples[key].extend(totals[identity] for identity in placed
                                if totals.get(identity) is not None)
            continue
        candidates = {identity for identity, total in totals.items() if total is not None
                      and (division == SpeakerResult.VARSITY or identity in novices)}
        use_ranks = all(ranks.get(identity) is not None for identity in candidates)
        keys = {identity: (totals[identity], -ranks[identity] if use_ranks else 0)
                for identity in candidates}
        ordered = sorted(keys.values(), reverse=True)
        if len(ordered) >= place:
            target = ordered[place - 1]
            samples[key].extend(totals[identity] for identity, score in keys.items() if score == target)


def _add_seed_samples(samples, group, candidates, totals, ranks, wins, unknown_outcomes):
    # A partial card elsewhere in the field must not veto every usable sample.
    candidates = {team for team in candidates
                  if team not in unknown_outcomes and totals.get(team) is not None}
    if not candidates:
        return
    use_ranks = all(ranks.get(team_id) is not None for team_id in candidates)
    keys = {team_id: (wins[team_id], totals[team_id], -ranks[team_id] if use_ranks else 0)
            for team_id in candidates}
    for label, target in (("top", max(keys.values())), ("bottom", min(keys.values()))):
        samples[f"{label}_{group}_seed"].extend(
            totals[team_id] for team_id, key in keys.items() if key == target
        )
