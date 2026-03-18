"""Core ELO math engine for individual and partner modes, including K-factor decay and partner delta rebalancing."""


import math

from core.utils.elo_runtime_engine.constants import INDIVIDUAL_MODE, normalize_school_name
from core.utils.elo_runtime_engine.models import PlayerStats


def expected_score(rating_a, rating_b):
    return 1.0 / (1.0 + math.pow(10.0, (rating_b - rating_a) / 400.0))


def k_factor_for_experience(results_played, k_max, k_min, decay_scale):
    if decay_scale <= 0:
        return k_min
    return k_min + (k_max - k_min) * math.exp(-results_played / decay_scale)


def average_rating(team, ratings, initial_rating):
    if not team:
        return initial_rating
    return sum(ratings.get(player_id, initial_rating) for player_id in team) / float(len(team))


def rebalance_team_deltas(team, pre_ratings, legacy_deltas, higher_share):
    if len(team) != 2:
        return legacy_deltas

    first, second = team
    first_rating = pre_ratings[first]
    second_rating = pre_ratings[second]
    if first_rating == second_rating:
        higher, lower = sorted((first, second))
    else:
        higher, lower = (first, second) if first_rating > second_rating else (second, first)

    higher_legacy = legacy_deltas.get(higher, 0.0)
    lower_legacy = legacy_deltas.get(lower, 0.0)
    team_total_delta = higher_legacy + lower_legacy
    share = max(0.0, min(1.0, float(higher_share)))

    if share >= 0.5:
        blend = (share - 0.5) / 0.5
        target_higher = team_total_delta
        target_lower = 0.0
    else:
        blend = (0.5 - share) / 0.5
        target_higher = 0.0
        target_lower = team_total_delta

    higher_final = higher_legacy + blend * (target_higher - higher_legacy)
    lower_final = lower_legacy + blend * (target_lower - lower_legacy)
    return {higher: higher_final, lower: lower_final}


def update_player_stats(player_stats, season, stage, won):
    player_stats.rounds += 1
    if won:
        player_stats.yearly_results[season][0] += 1
    else:
        player_stats.yearly_results[season][1] += 1

    if stage == "outround":
        player_stats.outround_rounds += 1
    else:
        player_stats.prelim_rounds += 1


def _seed_debate_players(debate, ratings, stats, initial_rating):
    participants = list(dict.fromkeys([*debate.team_a, *debate.team_b]))
    pre_ratings = {player_id: ratings.get(player_id, initial_rating) for player_id in participants}
    for player_id in participants:
        if player_id not in stats:
            stats[player_id] = PlayerStats()
        if player_id not in ratings:
            ratings[player_id] = initial_rating
    return pre_ratings


def _apply_school_hints(debate, stats):
    team_a_school = normalize_school_name(debate.team_a_school)
    team_b_school = normalize_school_name(debate.team_b_school)

    for player_id in debate.team_a:
        if team_a_school:
            stats[player_id].school_hints[team_a_school] += 1
            stats[player_id].school_hints_by_season[debate.season][team_a_school] += 1

    for player_id in debate.team_b:
        if team_b_school:
            stats[player_id].school_hints[team_b_school] += 1
            stats[player_id].school_hints_by_season[debate.season][team_b_school] += 1


def _apply_name_hints(debate, stats):
    for player_id, name in (debate.participant_names or {}).items():
        text = str(name or "").strip()
        if not text:
            continue
        stats[player_id].name_hints[text] += 1


def _record_season_snapshots(debate, stats, ratings):
    season = str(debate.season or "").strip()
    if not season:
        return

    participants = list(dict.fromkeys([*debate.team_a, *debate.team_b]))
    for player_id in participants:
        player_stats = stats.get(player_id)
        if player_stats is None:
            continue
        player_stats.rating = float(ratings.get(player_id, player_stats.rating))
        player_stats.season_snapshots[season] = {
            "elo": float(player_stats.rating),
            "rounds": int(round(player_stats.rounds)),
            "prelim_rounds": int(round(player_stats.prelim_rounds)),
            "outround_rounds": int(round(player_stats.outround_rounds)),
        }


def _apply_individual_mode(debate, stats, ratings, pre_ratings, k_max, k_min, k_decay_scale):
    processed = 0.0
    winners = debate.team_a if debate.winner == "a" else debate.team_b
    losers = debate.team_b if debate.winner == "a" else debate.team_a

    for winner in winners:
        winner_k_pre = k_factor_for_experience(stats[winner].rounds, k_max, k_min, k_decay_scale)
        for loser in losers:
            loser_k_pre = k_factor_for_experience(stats[loser].rounds, k_max, k_min, k_decay_scale)
            expected_winner = expected_score(pre_ratings[winner], pre_ratings[loser])
            winner_delta = winner_k_pre * (1.0 - expected_winner)
            loser_delta = loser_k_pre * (0.0 - (1.0 - expected_winner))

            ratings[winner] += winner_delta
            ratings[loser] += loser_delta

            winner_k_post = k_factor_for_experience(
                stats[winner].rounds + 1,
                k_max,
                k_min,
                k_decay_scale,
            )
            loser_k_post = k_factor_for_experience(
                stats[loser].rounds + 1,
                k_max,
                k_min,
                k_decay_scale,
            )

            update_player_stats(stats[winner], debate.season, debate.stage, True)
            update_player_stats(stats[loser], debate.season, debate.stage, False)
            processed += 1

    return processed


def _team_legacy_and_k(team, stats, k_max, k_min, k_decay_scale, team_delta):
    legacy = {}
    pre_k = {}
    post_k = {}

    for player_id in team:
        player_k = k_factor_for_experience(stats[player_id].rounds, k_max, k_min, k_decay_scale)
        pre_k[player_id] = player_k
        post_k[player_id] = k_factor_for_experience(
            stats[player_id].rounds + 1,
            k_max,
            k_min,
            k_decay_scale,
        )
        legacy[player_id] = player_k * team_delta

    return legacy, pre_k, post_k


def _apply_partner_mode(
    debate,
    stats,
    ratings,
    pre_ratings,
    initial_rating,
    k_max,
    k_min,
    k_decay_scale,
    higher_elo_win_share,
    higher_elo_loss_share,
):
    team_a_rating = average_rating(debate.team_a, pre_ratings, initial_rating)
    team_b_rating = average_rating(debate.team_b, pre_ratings, initial_rating)
    expected_a = expected_score(team_a_rating, team_b_rating)
    actual_a = 1.0 if debate.winner == "a" else 0.0

    team_a_legacy, team_a_pre_k, team_a_post_k = _team_legacy_and_k(
        debate.team_a,
        stats,
        k_max,
        k_min,
        k_decay_scale,
        actual_a - expected_a,
    )
    team_b_legacy, team_b_pre_k, team_b_post_k = _team_legacy_and_k(
        debate.team_b,
        stats,
        k_max,
        k_min,
        k_decay_scale,
        (1.0 - actual_a) - (1.0 - expected_a),
    )

    team_a_won = debate.winner == "a"
    team_a_adjusted = rebalance_team_deltas(
        debate.team_a,
        pre_ratings,
        team_a_legacy,
        higher_elo_win_share if team_a_won else higher_elo_loss_share,
    )
    team_b_adjusted = rebalance_team_deltas(
        debate.team_b,
        pre_ratings,
        team_b_legacy,
        higher_elo_win_share if not team_a_won else higher_elo_loss_share,
    )

    for player_id in debate.team_a:
        ratings[player_id] += team_a_adjusted.get(player_id, 0.0)
    for player_id in debate.team_b:
        ratings[player_id] += team_b_adjusted.get(player_id, 0.0)

    for player_id in debate.team_a:
        won = team_a_won
        update_player_stats(stats[player_id], debate.season, debate.stage, won)

    for player_id in debate.team_b:
        won = not team_a_won
        update_player_stats(stats[player_id], debate.season, debate.stage, won)

    return 1


def apply_elo(
    debates,
    initial_rating,
    k_max,
    k_min,
    k_decay_scale,
    mode,
    higher_elo_win_share,
    higher_elo_loss_share,
    debates_sorted=False,
):
    ratings = {}
    stats = {}
    processed = 0.0

    ordered_debates = (
        debates if debates_sorted else sorted(debates, key=lambda row: (row.timestamp, row.sort_key, row.tournament_key))
    )
    for debate in ordered_debates:
        pre_ratings = _seed_debate_players(debate, ratings, stats, initial_rating)
        _apply_name_hints(debate, stats)
        _apply_school_hints(debate, stats)

        if mode == INDIVIDUAL_MODE:
            processed += _apply_individual_mode(
                debate,
                stats,
                ratings,
                pre_ratings,
                k_max,
                k_min,
                k_decay_scale,
            )
        else:
            processed += _apply_partner_mode(
                debate,
                stats,
                ratings,
                pre_ratings,
                initial_rating,
                k_max,
                k_min,
                k_decay_scale,
                higher_elo_win_share,
                higher_elo_loss_share,
            )

        _record_season_snapshots(debate, stats, ratings)

    for player_id, rating in ratings.items():
        if player_id not in stats:
            stats[player_id] = PlayerStats()
        stats[player_id].rating = float(rating)

    return stats, int(round(processed))
