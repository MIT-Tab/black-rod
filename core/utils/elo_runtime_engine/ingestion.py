"""Transforms contract-backed rounds into deduplicated runtime Debate and TournamentSnapshot objects with minimal legacy fallback."""


from datetime import datetime, time, timezone
from collections import defaultdict
from django.core.exceptions import ObjectDoesNotExist

from core.models import Debater, Round, RoundStats
from core.utils.debater_aliases import load_representative_debater_maps
from core.utils.elo_runtime_engine.constants import (
    normalize_school_name,
    season_to_int,
    should_exclude_tournament,
)
from core.utils.elo_runtime_engine.debate_contract import (
    debate_round_label,
    debate_sort_key,
    debate_source_fields,
    debate_timestamp,
    debate_tournament_key,
    debate_tournament_name,
    debate_weight,
    is_rated_debate,
    metadata_team_ids,
    metadata_team_names,
    stage_for_rating,
    winner_code_for_debate,
)
from core.utils.elo_runtime_engine.models import Debate, TournamentSnapshot


def _debate_identity_key(tournament_id, stage, round_label, team_a, team_b, winner):
    return (
        tournament_id,
        stage,
        round_label,
        tuple(sorted(team_a)),
        tuple(sorted(team_b)),
        winner,
    )


def _forum_dedupe_key(debate):
    return (
        debate.tournament_key,
        debate.round_label,
        tuple(sorted((tuple(sorted(debate.team_a)), tuple(sorted(debate.team_b))))),
        debate.winner,
    )


def _participant_names_for_teams(team_a, names_a, team_b, names_b):
    participant_names = {}
    for index, debater_id in enumerate(team_a):
        participant_names[debater_id] = names_a[index] if index < len(names_a) else str(debater_id)
    for index, debater_id in enumerate(team_b):
        participant_names[debater_id] = names_b[index] if index < len(names_b) else str(debater_id)
    return participant_names


def _collapse_linked_team_ids(team_ids, team_names, representative_by_id):
    collapsed_ids = []
    collapsed_names = []
    for index, debater_id in enumerate(team_ids):
        resolved_id = int(debater_id)
        if resolved_id > 0:
            resolved_id = int(representative_by_id.get(resolved_id, resolved_id))
        speaker_name = str(team_names[index] or "") if index < len(team_names) else ""
        if resolved_id in collapsed_ids:
            existing_index = collapsed_ids.index(resolved_id)
            if speaker_name and not collapsed_names[existing_index]:
                collapsed_names[existing_index] = speaker_name
            continue
        collapsed_ids.append(resolved_id)
        collapsed_names.append(speaker_name)
    return tuple(collapsed_ids), tuple(collapsed_names)


def _has_contract_team_metadata(metadata):
    return bool(
        metadata_team_ids(metadata, "team_a_ids")
        and metadata_team_ids(metadata, "team_b_ids")
    )


def _imported_alias_rows(round_obj, side):
    try:
        imported_metadata = round_obj.imported_metadata
    except ObjectDoesNotExist:
        return ()

    alias_rows = (
        (imported_metadata.gov_1_alias, imported_metadata.gov_2_alias)
        if side == "a"
        else (imported_metadata.opp_1_alias, imported_metadata.opp_2_alias)
    )
    values = []
    for alias_row in alias_rows:
        if alias_row is None or alias_row.debater_id is None:
            continue
        values.append((int(alias_row.debater_id), str(alias_row.source_name or "").strip()))
    return tuple(values)


def _has_contract_team_identity(round_obj, metadata):
    return bool(
        _has_contract_team_metadata(metadata)
        or (_imported_alias_rows(round_obj, "a") and _imported_alias_rows(round_obj, "b"))
    )


def _round_is_in_scope(tournament, season, allowed_seasons, include_novice, include_proam, completed_only, max_date):
    if allowed_seasons and season not in allowed_seasons:
        return False
    if should_exclude_tournament(
        tournament,
        include_novice=include_novice,
        include_proam=include_proam,
    ):
        return False
    if completed_only and (not tournament.date or (max_date and tournament.date > max_date)):
        return False
    return True


def _team_from_metadata_or_round(metadata, round_obj, side, representative_by_id):
    imported_aliases = _imported_alias_rows(round_obj, side)
    if imported_aliases:
        team_ids = tuple(alias_row[0] for alias_row in imported_aliases)
        team_names = tuple(alias_row[1] for alias_row in imported_aliases)
        return _collapse_linked_team_ids(team_ids, team_names, representative_by_id)

    ids_key = "team_a_ids" if side == "a" else "team_b_ids"
    names_key = "team_a_names" if side == "a" else "team_b_names"

    team_ids = metadata_team_ids(metadata, ids_key)
    team_names = metadata_team_names(metadata, names_key)

    if team_ids:
        return _collapse_linked_team_ids(team_ids, team_names, representative_by_id)

    team = round_obj.gov if side == "a" else round_obj.opp
    if not team:
        return (), ()

    debaters = list(team.debaters.all())
    ids = [int(debater.id) for debater in debaters if debater.id]
    names = [str(debater.name or "").strip() for debater in debaters]
    return _collapse_linked_team_ids(ids, names, representative_by_id)


def _school_from_metadata_or_ids(metadata, ids_key, school_key, team_ids, debater_school_by_id):
    from_metadata = normalize_school_name(str(metadata.get(school_key) or "").strip())
    if from_metadata:
        return from_metadata
    if team_ids:
        return normalize_school_name(debater_school_by_id.get(int(team_ids[0])) or "")
    id_values = metadata_team_ids(metadata, ids_key)
    if id_values:
        return normalize_school_name(debater_school_by_id.get(int(id_values[0])) or "")
    return ""


def _round_schools_from_round(metadata, team_a, team_b, debater_school_by_id):
    team_a_school = _school_from_metadata_or_ids(
        metadata,
        "team_a_ids",
        "team_a_school",
        team_a,
        debater_school_by_id,
    )
    team_b_school = _school_from_metadata_or_ids(
        metadata,
        "team_b_ids",
        "team_b_school",
        team_b,
        debater_school_by_id,
    )
    return team_a_school, team_b_school


def _collapse_team_when_single_speaker_has_scores(team_ids, team_names, scored_debater_ids):
    if not team_ids or len(team_ids) < 2:
        return team_ids, team_names
    if not scored_debater_ids:
        return team_ids, team_names

    scored_indexes = [
        index
        for index, debater_id in enumerate(team_ids)
        if int(debater_id) > 0 and int(debater_id) in scored_debater_ids
    ]
    if len(scored_indexes) != 1:
        return team_ids, team_names

    scored_index = scored_indexes[0]
    scored_id = int(team_ids[scored_index])
    scored_name = team_names[scored_index] if scored_index < len(team_names) else str(scored_id)
    return (scored_id,), (str(scored_name or ""),)


def _load_representative_first_seasons(representative_by_id):
    first_season_by_representative = {}

    for debater_id, first_season in Debater.all_objects.values_list("id", "first_season"):
        if debater_id is None:
            continue
        representative_id = int(representative_by_id.get(int(debater_id), int(debater_id)))
        season_value = season_to_int(first_season)
        if season_value is None:
            continue
        current = first_season_by_representative.get(representative_id)
        if current is None or season_value < current:
            first_season_by_representative[representative_id] = season_value

    return first_season_by_representative


def _team_is_proam_partnership(team_ids, debate_season, representative_first_seasons):
    if len(team_ids) != 2:
        return False

    season_value = season_to_int(debate_season)
    if season_value is None:
        return False

    novice_flags = []
    for debater_id in team_ids:
        first_season = representative_first_seasons.get(int(debater_id))
        novice_flags.append(first_season is not None and first_season == season_value)
    return novice_flags.count(True) == 1


def _infer_is_proam_partnership(debate_season, team_a, team_b, representative_first_seasons):
    return _team_is_proam_partnership(team_a, debate_season, representative_first_seasons) or _team_is_proam_partnership(
        team_b,
        debate_season,
        representative_first_seasons,
    )


def _build_debate_from_round(
    round_obj,
    season,
    fallback_timestamp,
    debater_school_by_id,
    scored_debaters_by_round_id,
    representative_by_id,
    representative_first_seasons,
):
    winner = winner_code_for_debate(round_obj)
    if not is_rated_debate(round_obj):
        return None, None
    if winner not in {"a", "b"}:
        return None, None

    metadata = round_obj.metadata if isinstance(round_obj.metadata, dict) else {}
    has_contract_team_identity = _has_contract_team_identity(round_obj, metadata)
    team_a, names_a = _team_from_metadata_or_round(
        metadata,
        round_obj,
        "a",
        representative_by_id,
    )
    team_b, names_b = _team_from_metadata_or_round(
        metadata,
        round_obj,
        "b",
        representative_by_id,
    )
    if not has_contract_team_identity:
        scored_debater_ids = scored_debaters_by_round_id.get(int(round_obj.id)) or set()
        team_a, names_a = _collapse_team_when_single_speaker_has_scores(team_a, names_a, scored_debater_ids)
        team_b, names_b = _collapse_team_when_single_speaker_has_scores(team_b, names_b, scored_debater_ids)
    if not team_a or not team_b:
        return None, None

    stage = stage_for_rating(round_obj)
    round_label = debate_round_label(round_obj)
    key = _debate_identity_key(
        round_obj.tournament_id,
        stage,
        round_label,
        team_a,
        team_b,
        winner,
    )

    source_kind, source_label = debate_source_fields(round_obj)
    participant_names = _participant_names_for_teams(team_a, names_a, team_b, names_b)
    team_a_school, team_b_school = _round_schools_from_round(
        metadata,
        team_a,
        team_b,
        debater_school_by_id,
    )

    tournament = round_obj.tournament
    tournament_key = debate_tournament_key(
        round_obj,
        "%s:%s:%s" % (tournament.id, season, tournament.name),
    )
    tournament_name = debate_tournament_name(round_obj, tournament.name)

    debate = Debate(
        timestamp=debate_timestamp(round_obj, fallback_timestamp),
        tournament_key=tournament_key,
        tournament_name=tournament_name,
        season=season,
        stage=stage,
        sort_key=debate_sort_key(round_obj),
        round_label=round_label,
        team_a=team_a,
        team_b=team_b,
        winner=winner,
        source_kind=source_kind,
        source_label=source_label,
        participant_names=participant_names,
        team_a_school=team_a_school,
        team_b_school=team_b_school,
        weight=debate_weight(round_obj),
        is_proam_partnership=bool(
            metadata.get("is_proam_partnership", False)
            or _infer_is_proam_partnership(
                season,
                team_a,
                team_b,
                representative_first_seasons,
            )
        ),
    )
    return key, debate


def build_ingested_snapshots_and_debates(allowed_seasons, include_novice, include_proam, completed_only, max_date):
    rounds = list(
        Round.objects.select_related(
            "tournament",
            "gov",
            "opp",
            "imported_metadata",
            "imported_metadata__gov_1_alias",
            "imported_metadata__gov_2_alias",
            "imported_metadata__opp_1_alias",
            "imported_metadata__opp_2_alias",
        )
        .prefetch_related("imported_metadata__sources")
        .order_by(
            "tournament__date",
            "tournament_id",
            "round_number",
            "id",
        )
    )
    representative_by_id, _linked_ids_by_representative = load_representative_debater_maps()
    representative_first_seasons = _load_representative_first_seasons(representative_by_id)
    debater_school_by_id = {}
    representative_school_candidates = defaultdict(list)
    for debater_id, school_name, latest_season in Debater.all_objects.values_list(
        "id",
        "school__name",
        "latest_season",
    ):
        if debater_id is None:
            continue
        normalized_school = normalize_school_name(school_name)
        debater_id = int(debater_id)
        representative_id = int(representative_by_id.get(debater_id, debater_id))
        if normalized_school:
            debater_school_by_id[debater_id] = normalized_school
            representative_school_candidates[representative_id].append(
                (
                    str(latest_season or "").strip(),
                    normalized_school != "unaffiliated",
                    normalized_school,
                )
            )
    for representative_id, candidates in representative_school_candidates.items():
        preferred = sorted(candidates, key=lambda row: (row[1], row[0], row[2]))[-1]
        debater_school_by_id[representative_id] = preferred[2]
    manual_round_ids = []
    for round_obj in rounds:
        metadata = round_obj.metadata if isinstance(round_obj.metadata, dict) else {}
        if not _has_contract_team_identity(round_obj, metadata):
            manual_round_ids.append(int(round_obj.id))

    scored_debaters_by_round_id = defaultdict(set)
    if manual_round_ids:
        for round_id, debater_id in RoundStats.objects.filter(
            round_id__in=manual_round_ids,
            speaks__isnull=False,
        ).values_list("round_id", "debater_id"):
            if round_id is None or debater_id is None:
                continue
            resolved_id = int(representative_by_id.get(int(debater_id), int(debater_id)))
            scored_debaters_by_round_id[int(round_id)].add(resolved_id)

    snapshots = []
    debates = []
    seen_tournaments = set()
    seen_forum_keys = set()

    for round_obj in rounds:
        tournament = round_obj.tournament
        season = str(tournament.season or "").strip()
        if not _round_is_in_scope(
            tournament,
            season,
            allowed_seasons,
            include_novice,
            include_proam,
            completed_only,
            max_date,
        ):
            continue

        timestamp = datetime.combine(tournament.date, time(12, 0), tzinfo=timezone.utc)
        if tournament.id not in seen_tournaments:
            seen_tournaments.add(tournament.id)
            snapshots.append(
                TournamentSnapshot(
                    timestamp=timestamp,
                    tournament_id=tournament.id,
                    tournament_name=tournament.name,
                    season=season,
                )
            )

        key, debate = _build_debate_from_round(
            round_obj,
            season,
            timestamp,
            debater_school_by_id,
            scored_debaters_by_round_id,
            representative_by_id,
            representative_first_seasons,
        )
        if not key or debate is None:
            continue

        if debate.source_kind == "forum":
            forum_key = _forum_dedupe_key(debate)
            if forum_key in seen_forum_keys:
                continue
            seen_forum_keys.add(forum_key)

        debates.append(debate)

    snapshots.sort(key=lambda row: (row.timestamp, row.season, row.tournament_id))
    debates.sort(key=lambda row: (row.timestamp, row.sort_key, row.tournament_key))
    return snapshots, debates
