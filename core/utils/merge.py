from django.db import IntegrityError, transaction
from django.db.models import Q

from core.models import (
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
    Video,
)
from core.models.standings.toty import TOTY
from core.utils.rankings import (
    redo_rankings,
    update_noty,
    update_online_quals,
    update_qual_points,
    update_soty,
    update_toty,
)


class MergeError(Exception):
    """Raised when a merge operation cannot be completed."""


def _season_token(value):
    if value is None:
        return None
    return str(value).split("-", maxsplit=1)[0]


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
    if primary.pk == secondary.pk:
        raise MergeError("Cannot merge a debater into itself.")

    affected_teams = set(primary.teams.all()) | set(secondary.teams.all())

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
        RoundStats.objects.filter(debater=secondary).update(debater=primary)

        _merge_qual_points(primary, secondary, seasons)
        _merge_qualifications(primary, secondary)
        _merge_standings(primary, secondary)

        _merge_videos(primary, secondary)
        _merge_reaffs(primary, secondary)

        _merge_teams(primary, secondary)

        _update_primary_profile(primary, secondary)

        secondary.delete()

        _rerun_rankings(primary, affected_teams, seasons)

    return {
        "primary_id": primary.pk,
        "secondary_id": secondary.pk,
        "affected_team_ids": [team.pk for team in affected_teams],
        "seasons": sorted(seasons),
    }


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
                if result.tie and not conflict.tie:
                    conflict.tie = True
                    conflict.save(update_fields=["tie"])
            result.delete()


def _merge_qual_points(primary, secondary, seasons):
    if not seasons:
        return
    QualPoints.objects.filter(
        debater__in=[primary, secondary], season__in=seasons
    ).delete()


def _merge_qualifications(primary, secondary):
    for qual in list(secondary.quals.all()):
        qual.debater = primary
        try:
            qual.save()
        except IntegrityError:
            existing = QUAL.objects.filter(
                debater=primary,
                season=qual.season,
                qual_type=qual.qual_type,
            ).first()
            if existing and not existing.tournament and qual.tournament:
                existing.tournament = qual.tournament
                existing.save(update_fields=["tournament"])
            qual.delete()


def _merge_standings(primary, secondary):
    SOTY.objects.filter(debater=secondary).delete()
    NOTY.objects.filter(debater=secondary).delete()
    OnlineQUAL.objects.filter(debater=secondary).delete()


def _merge_videos(primary, secondary):
    Video.objects.filter(pm=secondary).update(pm=primary)
    Video.objects.filter(lo=secondary).update(lo=primary)
    Video.objects.filter(mg=secondary).update(mg=primary)
    Video.objects.filter(mo=secondary).update(mo=primary)


def _merge_reaffs(primary, secondary):
    Reaff.objects.filter(old_debater=secondary).update(old_debater=primary)
    Reaff.objects.filter(new_debater=secondary).update(new_debater=primary)


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


def _rerun_rankings(primary, affected_teams, seasons):
    for season in seasons:
        for team in affected_teams:
            update_toty(team, season=season)
            update_online_quals(team, season=season)
            update_qual_points(team, season=season)

        update_soty(primary, season=season)
        update_noty(primary, season=season)

        redo_rankings(
            TOTY.objects.filter(season=season),
            season=season,
            cache_type="toty",
        )
        redo_rankings(
            SOTY.objects.filter(season=season),
            season=season,
            cache_type="soty",
        )
        redo_rankings(
            NOTY.objects.filter(season=season),
            season=season,
            cache_type="noty",
        )
