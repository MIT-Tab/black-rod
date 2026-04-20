from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q

from core.models import (
    COTY,
    DebaterAlias,
    Debater,
    ImportedRoundJudge,
    ImportedRoundMetadata,
    MergeDebaterRequest,
    NOTY,
    OnlineQUAL,
    QUAL,
    QualPoints,
    Reaff,
    RoundStats,
    SOTY,
    SpeakerResult,
    Team,
    TeamResult,
    TOTY,
    TOTYReaff,
    Video,
    School,
)


class MergeError(Exception):
    """Raised when a merge operation cannot be completed."""


def _season_token(value):
    if value is None:
        return None
    return str(value).split("-", maxsplit=1)[0]


def _season_int(value):
    token = _season_token(value)
    if token is None:
        return None
    try:
        return int(token)
    except (TypeError, ValueError):
        return None


def _school_included_in_oty(debater):
    school = getattr(debater, "school", None)
    return bool(school and getattr(school, "included_in_oty", False))


def _is_online_season(season):
    normalized = _season_token(season)
    return normalized in {_season_token(value) for value in settings.ONLINE_SEASONS}


def _qual_bar_for_season(season):
    season_token = _season_token(season)
    historical_bars = getattr(settings, "HISTORICAL_QUAL_BARS", {})
    if season_token in historical_bars:
        try:
            return float(historical_bars[season_token])
        except (TypeError, ValueError):
            pass
    return float(settings.QUAL_BAR)


def _update_standing_place(standing_model, season, **_identity_lookup):
    season = str(season)
    standing_model.objects.filter(season=season, points=0).delete()

    season_rows = list(standing_model.objects.filter(season=season).order_by("-points", "pk"))
    if not season_rows:
        return

    place = 1
    index = 0

    while index < len(season_rows):
        current_points = season_rows[index].points
        tied_group = []

        while index < len(season_rows) and season_rows[index].points == current_points:
            tied_group.append(season_rows[index])
            index += 1

        tied = len(tied_group) > 1
        for standing in tied_group:
            fields_to_update = []
            if standing.place != place:
                standing.place = place
                fields_to_update.append("place")
            if standing.tied != tied:
                standing.tied = tied
                fields_to_update.append("tied")
            if fields_to_update:
                standing.save(update_fields=fields_to_update)

        place += len(tied_group)


def _reset_markers(standing, labels):
    for label in labels:
        setattr(standing, f"marker_{label}", 0)
        setattr(standing, f"tournament_{label}", None)


def _sync_points_qual(debater, season, qualified):
    season = str(season)
    if qualified:
        QUAL.objects.get_or_create(
            season=season,
            debater=debater,
            qual_type=QUAL.POINTS,
        )
        return

    QUAL.objects.filter(
        season=season,
        debater=debater,
        qual_type=QUAL.POINTS,
    ).delete()


def get_debater_result_counts(debater):
    """
    Return a dictionary summarising the results attached to a debater.
    """
    if debater is None:
        return None

    team_result_count = (
        TeamResult.objects.filter(team__debaters=debater)
        .distinct()
        .count()
    )
    speaker_result_count = debater.speaker_results.count()
    round_stat_count = debater.round_stats.count()
    video_count = Video.objects.filter(
        Q(pm=debater) | Q(lo=debater) | Q(mg=debater) | Q(mo=debater)
    ).count()

    return {
        "team_results": team_result_count,
        "speaker_results": speaker_result_count,
        "round_stats": round_stat_count,
        "videos": video_count,
        "total": team_result_count + speaker_result_count + round_stat_count + video_count,
    }


def merge_debaters(primary, secondary):
    """
    Merge all data from `secondary` into `primary`.

    Returns a dict with metadata about the merge that callers may use for logging.
    """
    # Validate that both debaters exist and have primary keys
    if not primary or not primary.pk:
        raise MergeError("Primary debater must have a valid primary key.")
    if not secondary or not secondary.pk:
        raise MergeError("Secondary debater must have a valid primary key.")
    
    if primary.pk == secondary.pk:
        raise MergeError("Cannot merge a debater into itself.")

    affected_teams = set(primary.teams.all()) | set(secondary.teams.all())
    affected_school_ids = {
        school_id for school_id in [primary.school_id, secondary.school_id] if school_id
    }

    seasons = set(
        TeamResult.objects.filter(team__in=affected_teams).values_list(
            "tournament__season", flat=True
        )
    )
    seasons.update(
        SpeakerResult.objects.filter(debater__in=[primary, secondary]).values_list(
            "tournament__season", flat=True
        )
    )
    seasons.update(primary.qual_points.values_list("season", flat=True))
    seasons.update(secondary.qual_points.values_list("season", flat=True))
    seasons.update(primary.quals.values_list("season", flat=True))
    seasons.update(secondary.quals.values_list("season", flat=True))
    seasons.update(primary.soty.values_list("season", flat=True))
    seasons.update(secondary.soty.values_list("season", flat=True))
    seasons.update(primary.noty.values_list("season", flat=True))
    seasons.update(secondary.noty.values_list("season", flat=True))
    seasons.update(primary.online_qual.values_list("season", flat=True))
    seasons.update(secondary.online_qual.values_list("season", flat=True))

    seasons = {str(season) for season in seasons if season}
    with transaction.atomic():
        _merge_speaker_results(primary, secondary)
        _merge_round_stats(primary, secondary)
        _merge_debater_aliases(primary, secondary)
        _merge_manual_elo_options(primary, secondary)

        _merge_qual_points(primary, secondary)
        _merge_qualifications(primary, secondary)
        _merge_standings(primary, secondary)

        _merge_videos(primary, secondary)
        _merge_reaffs(primary, secondary)

        _merge_teams(primary, secondary)

        _update_primary_profile(primary, secondary)

        # Cancel any other pending merge requests involving these debaters
        _cancel_pending_merge_requests(primary, secondary)

        secondary.delete()

        _recompute_merged_rankings(
            primary=primary,
            affected_teams=affected_teams,
            affected_school_ids=affected_school_ids,
            seasons=seasons,
        )

    return {
        "primary_id": primary.pk,
        "secondary_id": secondary.pk,
        "affected_team_ids": [team.pk for team in affected_teams],
        "seasons": sorted(seasons),
    }


def _merge_round_stats(primary, secondary):
    existing_rows = RoundStats.objects.filter(debater=primary).values(
        "round_id",
        "score_index",
    )
    occupied_slots = {
        (int(row["round_id"]), int(row["score_index"] or 1))
        for row in existing_rows
    }
    next_score_index_by_round = {}

    for stat in RoundStats.objects.filter(debater=secondary).order_by("round_id", "score_index", "id"):
        round_id = int(stat.round_id)
        score_index = int(stat.score_index or 1)
        desired_slot = (round_id, score_index)

        if desired_slot in occupied_slots:
            if round_id not in next_score_index_by_round:
                next_score_index_by_round[round_id] = (
                    max(
                        RoundStats.objects.filter(round_id=round_id, debater=primary).values_list(
                            "score_index",
                            flat=True,
                        ),
                        default=0,
                    )
                    + 1
                )
            score_index = next_score_index_by_round[round_id]
            next_score_index_by_round[round_id] += 1

        stat.debater = primary
        stat.score_index = score_index
        stat.save(update_fields=["debater", "score_index"])
        occupied_slots.add((round_id, score_index))


def _merge_debater_aliases(primary, secondary):
    for alias in DebaterAlias.objects.filter(debater=secondary).order_by("id"):
        existing = DebaterAlias.objects.filter(
            debater=primary,
            source_name=alias.source_name,
        ).first()
        if existing is not None:
            ImportedRoundMetadata.objects.filter(gov_1_alias=alias).update(gov_1_alias=existing)
            ImportedRoundMetadata.objects.filter(gov_2_alias=alias).update(gov_2_alias=existing)
            ImportedRoundMetadata.objects.filter(opp_1_alias=alias).update(opp_1_alias=existing)
            ImportedRoundMetadata.objects.filter(opp_2_alias=alias).update(opp_2_alias=existing)
            ImportedRoundJudge.objects.filter(debater_alias=alias).update(debater_alias=existing)
            alias.delete()
            continue
        DebaterAlias.objects.filter(pk=alias.pk).update(debater=primary)


def _merge_manual_elo_options(primary, secondary):
    primary_opt = str(getattr(primary, "elo_manual_opt", "") or "").strip().lower()
    secondary_opt = str(getattr(secondary, "elo_manual_opt", "") or "").strip().lower()
    if primary_opt:
        return
    if not secondary_opt:
        return
    Debater.objects.filter(pk=primary.pk).update(elo_manual_opt=secondary_opt)
    primary.elo_manual_opt = secondary_opt


def _merge_speaker_results(primary, secondary):
    for result in SpeakerResult.objects.filter(debater=secondary).select_related(
        "tournament"
    ):
        result.debater = primary
        try:
            result.save()
        except IntegrityError:
            conflict = SpeakerResult.objects.filter(
                debater=primary,
                tournament=result.tournament,
                type_of_place=result.type_of_place,
                place=result.place,
            ).first()
            if conflict:
                fields_to_update = []
                if result.tie and not conflict.tie:
                    conflict.tie = True
                    fields_to_update.append("tie")
                if result.counts_for_points and not conflict.counts_for_points:
                    conflict.counts_for_points = True
                    fields_to_update.append("counts_for_points")
                if fields_to_update:
                    conflict.save(update_fields=fields_to_update)
            result.delete()


def _merge_qual_points(primary, secondary):
    seasons = {
        str(season)
        for season in QualPoints.objects.filter(debater__in=[primary, secondary]).values_list(
            "season", flat=True
        )
        if season
    }

    for season in seasons:
        rows = list(
            QualPoints.objects.filter(debater__in=[primary, secondary], season=season)
            .order_by("id")
        )
        if not rows:
            continue

        primary_row = next((row for row in rows if row.debater_id == primary.id), None)
        merged_row = primary_row or rows[0]
        merged_points = sum(row.points for row in rows)

        fields_to_update = []
        if merged_row.debater_id != primary.id:
            merged_row.debater = primary
            fields_to_update.append("debater")
        if merged_row.points != merged_points:
            merged_row.points = merged_points
            fields_to_update.append("points")
        if fields_to_update:
            merged_row.save(update_fields=fields_to_update)

        if not _is_online_season(season):
            _sync_points_qual(
                debater=primary,
                season=season,
                qualified=merged_points >= _qual_bar_for_season(season),
            )

        QualPoints.objects.filter(debater__in=[primary, secondary], season=season).exclude(
            pk=merged_row.pk
        ).delete()


def _merge_qualifications(primary, secondary):
    for qual in list(secondary.quals.all()):
        existing = QUAL.objects.filter(
            debater=primary,
            season=qual.season,
            qual_type=qual.qual_type,
        ).first()

        if existing:
            fields_to_update = []

            if qual.tournament and not existing.tournament:
                existing.tournament = qual.tournament
                fields_to_update.append("tournament")

            if qual.place != -1 and (existing.place == -1 or qual.place < existing.place):
                existing.place = qual.place
                fields_to_update.append("place")

            if qual.points != -1 and qual.points > existing.points:
                existing.points = qual.points
                fields_to_update.append("points")

            if qual.tied and not existing.tied:
                existing.tied = True
                fields_to_update.append("tied")

            if fields_to_update:
                existing.save(update_fields=fields_to_update)

            qual.delete()
            continue

        qual.debater = primary
        qual.save()


def _merge_standings(primary, secondary):
    for standing_model in (SOTY, NOTY, OnlineQUAL):
        for standing in list(standing_model.objects.filter(debater=secondary)):
            existing = standing_model.objects.filter(
                debater=primary,
                season=standing.season,
            ).first()

            if not existing:
                standing.debater = primary
                standing.save(update_fields=["debater"])
                continue

            fields_to_update = []
            if standing.points > existing.points:
                existing.points = standing.points
                fields_to_update.append("points")

            if standing.place != -1 and (
                existing.place == -1 or standing.place < existing.place
            ):
                existing.place = standing.place
                fields_to_update.append("place")

            if standing.tied and not existing.tied:
                existing.tied = True
                fields_to_update.append("tied")

            if fields_to_update:
                existing.save(update_fields=fields_to_update)

            standing.delete()


def _recompute_merged_rankings(primary, affected_teams, affected_school_ids, seasons):
    if not seasons:
        return

    team_ids = [team.pk for team in affected_teams if team.pk]
    teams = Team.objects.filter(pk__in=team_ids).distinct()
    schools = School.objects.filter(pk__in=affected_school_ids).distinct()

    for season in seasons:
        for team in teams:
            _recompute_toty(team=team, season=season)

        _recompute_qual_points_and_quals(debater=primary, season=season)
        _recompute_online_qual(debater=primary, season=season)
        _recompute_soty(debater=primary, season=season)
        _recompute_noty(debater=primary, season=season)

        for school in schools:
            _recompute_coty(school=school, season=season)


def _recompute_toty(team, season):
    season = str(season)

    if team.hybrid or team.debaters.count() == 0:
        deleted, _ = TOTY.objects.filter(season=season, team=team).delete()
        if deleted:
            _update_standing_place(TOTY, season)
        return

    if TOTYReaff.objects.filter(old_team=team, season=season).exists():
        deleted, _ = TOTY.objects.filter(season=season, team=team).delete()
        if deleted:
            _update_standing_place(TOTY, season)
        return

    first_debater = team.debaters.first()
    if first_debater and not _school_included_in_oty(first_debater):
        deleted, _ = TOTY.objects.filter(season=season, team=team).delete()
        if deleted:
            _update_standing_place(TOTY, season)
        return

    results = (
        team.team_results.filter(tournament__season=season)
        .filter(tournament__toty=True)
        .filter(type_of_place=Debater.VARSITY)
        .filter(counts_for_points=True)
    )
    reaff = TOTYReaff.objects.filter(new_team=team, season=season).first()
    if reaff:
        results = results | (
            reaff.old_team.team_results.filter(tournament__season=season)
            .filter(tournament__toty=True)
            .filter(type_of_place=Debater.VARSITY)
            .filter(counts_for_points=True)
        )

    markers = [
        (
            result.tournament.get_toty_points(
                result.place, ghost_points=result.ghost_points
            ),
            result,
        )
        for result in results
    ]
    markers.sort(key=lambda marker: marker[0], reverse=True)

    toty = TOTY.objects.filter(season=season, team=team).first()
    if not markers:
        if toty:
            toty.delete()
            _update_standing_place(TOTY, season)
        return

    if not toty:
        toty = TOTY.objects.create(season=season, team=team)

    labels = ["one", "two", "three", "four", "five", "six"]
    _reset_markers(toty, labels)

    points = 0
    for index, marker in enumerate(markers[:5]):
        points_value, result = marker
        if not result.tournament:
            continue
        label = labels[index]
        setattr(toty, f"marker_{label}", points_value)
        setattr(toty, f"tournament_{label}", result.tournament)
        points += points_value

    toty.points = points
    toty.save()
    _update_standing_place(TOTY, season, team=team)


def _recompute_soty(debater, season):
    season = str(season)

    if not _school_included_in_oty(debater):
        deleted, _ = SOTY.objects.filter(season=season, debater=debater).delete()
        if deleted:
            _update_standing_place(SOTY, season)
        return

    results = (
        debater.speaker_results.filter(tournament__season=season)
        .filter(tournament__soty=True)
        .filter(type_of_place=Debater.VARSITY)
        .filter(counts_for_points=True)
    )
    reaff = Reaff.objects.filter(new_debater=debater, season=season).first()
    if reaff:
        results = results | (
            reaff.old_debater.speaker_results.filter(tournament__season=season)
            .filter(tournament__soty=True)
            .filter(type_of_place=Debater.VARSITY)
            .filter(counts_for_points=True)
        )

    markers = [
        (
            result.tournament.get_soty_points(result.place - (1 if result.tie else 0)),
            result,
        )
        for result in results
    ]
    markers.sort(key=lambda marker: marker[0], reverse=True)

    soty = SOTY.objects.filter(season=season, debater=debater).first()
    if not markers:
        if soty:
            soty.delete()
            _update_standing_place(SOTY, season)
        return

    if not soty:
        soty = SOTY.objects.create(season=season, debater=debater)

    labels = ["one", "two", "three", "four", "five", "six"]
    _reset_markers(soty, labels)

    points = 0
    for index, marker in enumerate(markers[:6]):
        points_value, result = marker
        if not result.tournament:
            continue
        label = labels[index]
        setattr(soty, f"marker_{label}", points_value)
        setattr(soty, f"tournament_{label}", result.tournament)
        points += points_value

    soty.points = points
    soty.save()
    _update_standing_place(SOTY, season, debater=debater)


def _recompute_noty(debater, season):
    season_int = _season_int(season)
    if season_int is None:
        return

    if season_int > settings.LAST_NOTY_SEASON:
        return

    season = str(season_int)
    if not _school_included_in_oty(debater):
        deleted, _ = NOTY.objects.filter(season=season, debater=debater).delete()
        if deleted:
            _update_standing_place(NOTY, season)
        return

    results = (
        debater.speaker_results.filter(tournament__season=season)
        .filter(tournament__noty=True)
        .filter(type_of_place=Debater.NOVICE)
    )
    markers = [
        (result.tournament.get_noty_points(result.place), result) for result in results
    ]
    markers.sort(key=lambda marker: marker[0], reverse=True)

    noty = NOTY.objects.filter(season=season, debater=debater).first()
    if not markers:
        if noty:
            noty.delete()
            _update_standing_place(NOTY, season)
        return

    if not noty:
        noty = NOTY.objects.create(season=season, debater=debater)

    labels = ["one", "two", "three", "four", "five", "six"]
    _reset_markers(noty, labels)

    points = 0
    for index, marker in enumerate(markers[:5]):
        points_value, result = marker
        if not result.tournament:
            continue
        label = labels[index]
        setattr(noty, f"marker_{label}", points_value)
        setattr(noty, f"tournament_{label}", result.tournament)
        points += points_value

    noty.points = points
    noty.save()
    _update_standing_place(NOTY, season, debater=debater)


def _recompute_online_qual(debater, season):
    season = str(season)

    if not _school_included_in_oty(debater):
        deleted, _ = OnlineQUAL.objects.filter(season=season, debater=debater).delete()
        if deleted:
            _update_standing_place(OnlineQUAL, season)
        if _is_online_season(season):
            _sync_points_qual(debater=debater, season=season, qualified=False)
        return

    results = (
        TeamResult.objects.filter(tournament__season=season)
        .filter(type_of_place=Debater.VARSITY)
        .filter(team__debaters=debater)
        .filter(counts_for_points=True)
    )
    markers = [
        (result.tournament.get_online_qual_points(result.place), result)
        for result in results
    ]
    markers.sort(key=lambda marker: marker[0], reverse=True)

    online_qual = OnlineQUAL.objects.filter(season=season, debater=debater).first()
    if not markers:
        if online_qual:
            online_qual.delete()
            _update_standing_place(OnlineQUAL, season)
        if _is_online_season(season):
            _sync_points_qual(debater=debater, season=season, qualified=False)
        return

    if not online_qual:
        online_qual = OnlineQUAL.objects.create(season=season, debater=debater)

    labels = ["one", "two", "three", "four", "five", "six"]
    _reset_markers(online_qual, labels)

    points = 0
    for index, marker in enumerate(markers[:6]):
        points_value, result = marker
        if not result.tournament:
            continue
        label = labels[index]
        setattr(online_qual, f"marker_{label}", points_value)
        setattr(online_qual, f"tournament_{label}", result.tournament)
        points += points_value

    online_qual.points = points
    online_qual.save()
    _update_standing_place(OnlineQUAL, season, debater=debater)

    if _is_online_season(season):
        _sync_points_qual(
            debater=debater,
            season=season,
            qualified=points >= settings.ONLINE_QUAL_BAR,
        )


def _recompute_qual_points_and_quals(debater, season):
    season = str(season)
    all_results = (
        TeamResult.objects.filter(tournament__season=season)
        .filter(type_of_place=Debater.VARSITY)
        .filter(team__debaters=debater)
    )
    if not _school_included_in_oty(debater):
        QualPoints.objects.filter(season=season, debater=debater).delete()
        if not _is_online_season(season):
            _sync_points_qual(debater=debater, season=season, qualified=False)
        return
    if not all_results.exists():
        return
    scoring_results = all_results.filter(counts_for_points=True)

    for result in scoring_results:
        if result.place == -1 or result.place > result.tournament.autoqual_bar:
            continue

        qual, created = QUAL.objects.get_or_create(
            season=season,
            debater=debater,
            qual_type=result.tournament.qual_type,
            defaults={"tournament": result.tournament},
        )
        if not created and result.tournament and not qual.tournament:
            qual.tournament = result.tournament
            qual.save(update_fields=["tournament"])

    points = sum(
        result.tournament.get_qual_points(
            result.place, ghost_points=result.ghost_points
        )
        for result in scoring_results.filter(tournament__qual=True)
    )

    qual_points = QualPoints.objects.filter(season=season, debater=debater).first()
    if points > 0:
        if not qual_points:
            qual_points = QualPoints.objects.create(season=season, debater=debater)
        qual_points.points = points
        qual_points.save(update_fields=["points"])
    elif qual_points:
        qual_points.delete()

    if not _is_online_season(season):
        _sync_points_qual(
            debater=debater,
            season=season,
            qualified=points >= _qual_bar_for_season(season),
        )


def _recompute_coty(school, season):
    season = str(season)
    coty = COTY.objects.filter(season=season, school=school).first()

    if _is_online_season(season) or not school.included_in_oty:
        if coty:
            coty.delete()
            _update_standing_place(COTY, season)
        return

    relevant_qual_points = QualPoints.objects.filter(
        season=season,
        debater__school=school,
    )
    points = sum(min(60, qual_points.points) for qual_points in relevant_qual_points)
    qualled_debaters = (
        QUAL.objects.filter(season=season, debater__school=school)
        .values("debater")
        .distinct()
        .count()
    )
    points += qualled_debaters * 6

    if not coty:
        coty = COTY.objects.create(season=season, school=school)
    coty.points = points
    coty.save(update_fields=["points"])
    _update_standing_place(COTY, season, school=school)


def _merge_videos(primary, secondary):
    Video.objects.filter(pm=secondary).update(pm=primary)
    Video.objects.filter(lo=secondary).update(lo=primary)
    Video.objects.filter(mg=secondary).update(mg=primary)
    Video.objects.filter(mo=secondary).update(mo=primary)


def _merge_reaffs(primary, secondary):
    Reaff.objects.filter(old_debater=secondary).update(old_debater=primary)
    Reaff.objects.filter(new_debater=secondary).update(new_debater=primary)


def _cancel_pending_merge_requests(primary, secondary):
    """Cancel any pending merge requests involving the debaters being merged."""
    from django.utils import timezone
    
    # Find all pending requests involving either debater
    pending_requests = MergeDebaterRequest.objects.filter(
        status=MergeDebaterRequest.STATUS_PENDING
    ).filter(
        Q(primary_debater=primary) | Q(secondary_debater=primary) |
        Q(primary_debater=secondary) | Q(secondary_debater=secondary)
    )
    
    # Mark them as auto-denied
    for request in pending_requests:
        request.status = MergeDebaterRequest.STATUS_DENIED
        request.denial_reason = f"Auto-cancelled: One of these debaters was merged in another request."
        request.processed_at = timezone.now()
        request.processed_by = None  # System action
        request.save()


def _merge_teams(primary, secondary):
    teams = Team.objects.filter(debaters=secondary).distinct()
    for team in teams:
        team.debaters.add(primary)
        team.debaters.remove(secondary)
        team.refresh_from_db()
        team.update_name()
        team.save()


def _update_primary_profile(primary, secondary):
    first_seasons = [
        season
        for season in [primary.first_season, secondary.first_season]
        if season
    ]
    latest_seasons = [
        season
        for season in [primary.latest_season, secondary.latest_season]
        if season
    ]

    if first_seasons:
        primary.first_season = min(first_seasons)
    if latest_seasons:
        primary.latest_season = max(latest_seasons)

    if secondary.school and not primary.school:
        primary.school = secondary.school

    primary.save()
