"""Builds the minimal debater profile context needed for runtime ELO filtering and dashboard rows."""


from collections import defaultdict

from core.models import Debater, QUAL, RoundStats
from core.utils.elo_runtime_engine.constants import normalize_school_name, season_to_int, to_int
from core.utils.elo_runtime_engine.models import DebaterProfile, DebaterRankingRow


def _linked_debater_context(player_ids):
    base_debaters = {
        row.id: row
        for row in Debater.all_objects.filter(id__in=player_ids).select_related("school")
    }
    alias_group_ids = {row.alias_group_id for row in base_debaters.values() if row.alias_group_id}

    linked_debaters = {}
    if alias_group_ids:
        linked_debaters = {
            row.id: row
            for row in Debater.all_objects.filter(alias_group_id__in=alias_group_ids).select_related("school")
        }

    all_debaters = dict(linked_debaters)
    all_debaters.update(base_debaters)

    alias_group_to_ids = defaultdict(set)
    for debater in all_debaters.values():
        if debater.alias_group_id:
            alias_group_to_ids[int(debater.alias_group_id)].add(int(debater.id))

    linked_ids_by_player = {}
    for player_id in player_ids:
        debater = base_debaters.get(player_id) or all_debaters.get(player_id)
        if not debater:
            linked_ids_by_player[player_id] = {player_id}
            continue
        if debater.alias_group_id:
            linked_ids_by_player[player_id] = set(alias_group_to_ids.get(int(debater.alias_group_id), {player_id}))
        else:
            linked_ids_by_player[player_id] = {player_id}

    all_linked_ids = set()
    for linked_ids in linked_ids_by_player.values():
        all_linked_ids.update(linked_ids)

    return base_debaters, all_debaters, linked_ids_by_player, all_linked_ids


def _load_qual_context(all_linked_ids):
    qual_data_available = QUAL.objects.exists()
    qual_rows = list(QUAL.objects.filter(debater_id__in=all_linked_ids).values("debater_id"))
    qual_ids = {int(row["debater_id"]) for row in qual_rows if row.get("debater_id") is not None}
    return qual_data_available, qual_ids


def _load_manual_opt_context(all_linked_ids):
    opt_rows = list(
        Debater.all_objects.filter(id__in=all_linked_ids).values(
            "id",
            "elo_manual_opt",
        )
    )
    return {
        int(row["id"]): str(row.get("elo_manual_opt") or "").strip().lower()
        for row in opt_rows
        if row.get("id") is not None
    }


def _load_appearances_by_debater(all_linked_ids):
    appearances_by_debater = defaultdict(list)
    appearance_rows = (
        RoundStats.objects.filter(debater_id__in=all_linked_ids)
        .select_related("round__tournament")
        .values(
            "debater_id",
            "round__tournament_id",
            "round__tournament__season",
        )
    )

    for row in appearance_rows:
        debater_id = to_int(row.get("debater_id"))
        if debater_id is None:
            continue
        appearances_by_debater[debater_id].append(
            {
                "season": str(row.get("round__tournament__season") or "").strip(),
                "tournament_id": to_int(row.get("round__tournament_id")),
            }
        )
    return appearances_by_debater


def _profile_season_values(linked_rows):
    primary_debater_id = None
    season_values = []

    for row in linked_rows:
        row_id = to_int(getattr(row, "id", None))
        if primary_debater_id is None and row_id is not None and row_id > 0:
            primary_debater_id = row_id
        first = season_to_int(row.first_season)
        latest = season_to_int(row.latest_season)
        if first is not None:
            season_values.append(first)
        if latest is not None:
            season_values.append(latest)

    return primary_debater_id, season_values


def _linked_schools(linked_rows):
    schools_by_id = {}
    for row in linked_rows:
        if not getattr(row, "school_id", None) or not getattr(row, "school", None):
            continue
        school_name = str(row.school.name or "").strip()
        if not school_name or normalize_school_name(school_name) == "unaffiliated":
            continue
        schools_by_id[int(row.school_id)] = school_name
    return [
        {"id": school_id, "name": school_name}
        for school_id, school_name in sorted(
            schools_by_id.items(),
            key=lambda item: item[1].lower(),
        )
    ]


def _affiliated_active_seasons(linked_rows):
    seasons = set()
    for row in linked_rows:
        school_name = normalize_school_name(getattr(getattr(row, "school", None), "name", ""))
        if not school_name or school_name == "unaffiliated":
            continue
        first = season_to_int(getattr(row, "first_season", None))
        latest = season_to_int(getattr(row, "latest_season", None))
        if first is None and latest is None:
            continue
        if first is None:
            first = latest
        if latest is None:
            latest = first
        if first is None or latest is None:
            continue
        if latest < first:
            first, latest = latest, first
        for season in range(first, latest + 1):
            seasons.add(str(season))
    return seasons


def _latest_affiliated_season(linked_rows):
    latest = None
    for row in linked_rows:
        school_name = normalize_school_name(getattr(getattr(row, "school", None), "name", ""))
        if not school_name or school_name == "unaffiliated":
            continue
        first = season_to_int(getattr(row, "first_season", None))
        row_latest = season_to_int(getattr(row, "latest_season", None))
        candidate = row_latest if row_latest is not None else first
        if candidate is None:
            continue
        if latest is None or candidate > latest:
            latest = candidate
    return str(latest) if latest is not None else None


def _appearance_tournaments_by_season(appearances, season_values):
    season_to_tournaments = defaultdict(set)

    for appearance in appearances:
        season = str(appearance.get("season") or "").strip()
        if not season:
            continue

        season_to_tournaments[season].add(appearance.get("tournament_id"))
        season_int = season_to_int(season)
        if season_int is not None:
            season_values.append(season_int)

    return season_to_tournaments


def _first_year_tournament_count(first_season, season_to_tournaments):
    if not first_season or first_season not in season_to_tournaments:
        return 0
    return len(
        {
            tournament_id
            for tournament_id in season_to_tournaments[first_season]
            if tournament_id is not None
        }
    )


def _build_profile_for_player(
    player_id,
    base_debaters,
    all_debaters,
    linked_ids_by_player,
    appearances_by_debater,
    qual_ids,
):
    linked_ids = linked_ids_by_player.get(player_id, {player_id})
    linked_rows = [all_debaters[row_id] for row_id in sorted(linked_ids) if row_id in all_debaters]
    base = base_debaters.get(player_id) or (linked_rows[0] if linked_rows else None)

    primary_debater_id, season_values = _profile_season_values(linked_rows)
    schools = _linked_schools(linked_rows)
    affiliated_active_seasons = _affiliated_active_seasons(linked_rows)
    latest_affiliated_season = _latest_affiliated_season(linked_rows)

    appearances = []
    for linked_id in linked_ids:
        appearances.extend(appearances_by_debater.get(linked_id, []))

    season_to_tournaments = _appearance_tournaments_by_season(appearances, season_values)

    first_season = str(min(season_values)) if season_values else None
    latest_season = str(max(season_values)) if season_values else None
    first_year_tournament_count = _first_year_tournament_count(first_season, season_to_tournaments)

    has_nat_qual = any(linked_id in qual_ids for linked_id in linked_ids)
    display_name = str(base.name if base else "Debater %s" % player_id).strip()
    school_name = ", ".join(
        school.get("name", "").strip()
        for school in schools
        if str(school.get("name", "")).strip()
    )

    return DebaterProfile(
        player_id=player_id,
        display_name=display_name or ("Debater %s" % player_id),
        primary_debater_id=primary_debater_id,
        school_name=school_name,
        schools=schools,
        first_season=first_season,
        latest_season=latest_season,
        first_year_tournament_count=first_year_tournament_count,
        has_nat_qual=has_nat_qual,
        affiliated_active_seasons=affiliated_active_seasons,
        latest_affiliated_season=latest_affiliated_season,
    )


def _load_profiles(player_ids):
    if not player_ids:
        return {}, False, {}, {}

    (
        base_debaters,
        all_debaters,
        linked_ids_by_player,
        all_linked_ids,
    ) = _linked_debater_context(player_ids)
    qual_data_available, qual_ids = _load_qual_context(all_linked_ids)
    manual_opts_by_debater = _load_manual_opt_context(all_linked_ids)
    appearances_by_debater = _load_appearances_by_debater(all_linked_ids)

    profiles = {}
    for player_id in player_ids:
        profiles[player_id] = _build_profile_for_player(
            player_id,
            base_debaters,
            all_debaters,
            linked_ids_by_player,
            appearances_by_debater,
            qual_ids,
        )

    return profiles, qual_data_available, linked_ids_by_player, manual_opts_by_debater


def _is_default_opt_out(profile, reference_season):
    if profile is None:
        return False
    if profile.has_nat_qual is False:
        return True
    first_season = season_to_int(profile.first_season)
    reference = season_to_int(reference_season)
    if first_season is None or reference is None:
        return False
    season_delta = reference - first_season
    if season_delta <= 0:
        return True
    if season_delta == 1 and profile.first_year_tournament_count < 3:
        return True
    return False


def _resolve_manual_opt(linked_ids, manual_opts_by_debater):
    states = {
        str(manual_opts_by_debater.get(linked_id) or "").strip().lower()
        for linked_id in linked_ids
    }
    if "opt_out" in states:
        return "opt_out"
    if "opt_in" in states:
        return "opt_in"
    return "unset"


def _resolve_effective_opt_out(default_opt_out, qual_data_available, manual_opt):
    if manual_opt == "opt_in":
        return False
    if manual_opt == "opt_out":
        return True
    return bool(qual_data_available and default_opt_out)


def _has_activity_in_active_seasons(profile, active_seasons):
    if not active_seasons:
        return True
    if profile is None:
        return False
    # The active board is gated by canonical affiliated seasons, not imported round
    # appearances, so alumni do not stay visible just because imported late-season data exists.
    affiliated_active_seasons = profile.affiliated_active_seasons or set()
    return any(season in affiliated_active_seasons for season in active_seasons)


def _preferred_display_name(player_stats, profile, player_id):
    if player_stats.name_hints:
        ordered_hints = sorted(
            player_stats.name_hints.items(),
            key=lambda row: (-float(row[1]), str(row[0]).lower()),
        )
        top_name = str(ordered_hints[0][0] or "").strip()
        if top_name:
            return top_name
    if profile is not None:
        return str(profile.display_name or "").strip() or ("Debater %s" % player_id)
    player_id_value = to_int(player_id)
    if player_id_value is not None and player_id_value < 0:
        return ""
    return "Debater %s" % player_id


def _display_snapshot_for_row(player_stats, profile, exclude_dino_rounds):
    current = {
        "elo": float(player_stats.rating),
        "rounds": int(round(player_stats.rounds)),
        "prelim_rounds": int(round(player_stats.prelim_rounds)),
        "outround_rounds": int(round(player_stats.outround_rounds)),
    }
    if not exclude_dino_rounds or profile is None:
        return current

    latest_affiliated_season = str(profile.latest_affiliated_season or "").strip()
    latest_profile_season = str(profile.latest_season or "").strip()
    if (
        not latest_affiliated_season
        or not latest_profile_season
        or latest_affiliated_season == latest_profile_season
    ):
        return current

    snapshot = player_stats.season_snapshots.get(latest_affiliated_season)
    if not isinstance(snapshot, dict):
        return current

    return {
        "elo": float(snapshot.get("elo", current["elo"])),
        "rounds": int(snapshot.get("rounds", current["rounds"])),
        "prelim_rounds": int(snapshot.get("prelim_rounds", current["prelim_rounds"])),
        "outround_rounds": int(snapshot.get("outround_rounds", current["outround_rounds"])),
    }


def build_dashboard_payload(
    stats,
    min_rounds,
    min_outrounds,
    output_limit,
    active_seasons,
    exclude_dino_rounds=False,
):
    active_season_set = {str(season).strip() for season in active_seasons if str(season).strip()}
    player_ids = set(stats.keys())
    (
        profiles,
        qual_data_available,
        linked_ids_by_player,
        manual_opts_by_debater,
    ) = _load_profiles(player_ids)

    excluded_default_opt_out_debaters = 0
    rows = []
    used_names = set()

    for player_id, player_stats in stats.items():
        profile = profiles.get(player_id)
        display_snapshot = _display_snapshot_for_row(
            player_stats,
            profile,
            exclude_dino_rounds=exclude_dino_rounds,
        )
        rounds_count = int(display_snapshot["rounds"])
        outrounds_count = int(display_snapshot["outround_rounds"])
        if rounds_count < int(min_rounds):
            continue
        if outrounds_count < int(min_outrounds):
            continue

        if not _has_activity_in_active_seasons(profile, active_season_set):
            continue

        reference_season = profile.latest_season if profile is not None else (
            max(player_stats.yearly_results.keys()) if player_stats.yearly_results else None
        )
        default_opt_out = _is_default_opt_out(profile, reference_season)
        manual_opt = _resolve_manual_opt(
            linked_ids=linked_ids_by_player.get(player_id, {player_id}),
            manual_opts_by_debater=manual_opts_by_debater,
        )
        effective_opt_out = _resolve_effective_opt_out(
            default_opt_out=default_opt_out,
            qual_data_available=qual_data_available,
            manual_opt=manual_opt,
        )
        if effective_opt_out:
            excluded_default_opt_out_debaters += 1
            continue

        base_name = _preferred_display_name(player_stats, profile, player_id)
        if not str(base_name).strip():
            continue
        display_name = base_name
        if display_name in used_names:
            display_name = "%s (%s)" % (base_name, player_id)
        used_names.add(display_name)

        debater_id = profile.primary_debater_id if profile is not None else None
        if debater_id is None:
            player_id_value = to_int(player_id)
            if player_id_value is not None and player_id_value > 0:
                debater_id = player_id_value

        row = DebaterRankingRow(
            rank=0,
            name=display_name,
            school_name=(profile.school_name if profile is not None else ""),
            schools=(list(profile.schools) if profile is not None else []),
            debater_id=debater_id,
            elo=float(display_snapshot["elo"]),
            rounds=rounds_count,
            prelim_rounds=int(display_snapshot["prelim_rounds"]),
            outround_rounds=int(display_snapshot["outround_rounds"]),
        )
        rows.append(row)

    rows.sort(key=lambda row: (-row.elo, -row.rounds, row.name.lower()))

    for index, row in enumerate(rows, start=1):
        row.rank = index
    if output_limit and output_limit > 0:
        rows = rows[:output_limit]

    return rows, excluded_default_opt_out_debaters, qual_data_available
