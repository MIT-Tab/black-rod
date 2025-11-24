import json
from statistics import median

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Avg, Q, Sum, F, Prefetch
from django.shortcuts import render
from django.urls import reverse
from core.models import (
    COTY, NOTY, SOTY, TOTY, OnlineQUAL,
    Tournament, Debater, School, Team, TeamResult, SpeakerResult
)

def index(request):
    seasons = settings.SEASONS
    current_season = request.GET.get("season", settings.CURRENT_SEASON)
    if current_season not in [s[0] for s in seasons]:
        current_season = settings.CURRENT_SEASON

    default = request.GET.get("default", "toty")
    if default not in ["toty", "coty", "soty", "noty"]:
        default = "toty"

    toty = TOTY.objects.filter(season=current_season).order_by("-points")
    coty = COTY.objects.filter(season=current_season).order_by("-points")
    soty = SOTY.objects.filter(season=current_season).order_by("-points")
    noty = NOTY.objects.filter(season=current_season).order_by("-points")

    using_online_quals = False
    online_quals = None
    online_seasons = None
    online_qual_bar = None

    if current_season in settings.ONLINE_SEASONS:
        using_online_quals = True
        online_quals = OnlineQUAL.objects.filter(season=current_season).order_by(
            "-points"
        )
        online_seasons = [
            (season[0], season[1])
            for season in seasons
            if season[0] in settings.ONLINE_SEASONS
        ]
        online_qual_bar = settings.ONLINE_QUAL_BAR

    render_noty = int(current_season) <= settings.LAST_NOTY_SEASON

    return render(
        request,
        "core/index.html",
        {
            "seasons": seasons,
            "current_season": current_season,
            "default": default,
            "toty": toty,
            "coty": coty,
            "soty": soty,
            "noty": noty,
            "render_noty": render_noty,
            "using_online_quals": using_online_quals,
            "online_quals": online_quals,
            "online_seasons": online_seasons,
            "online_qual_bar": online_qual_bar,
        },
    )


def standings_replay(request):
    seasons = settings.SEASONS
    current_season = request.GET.get("season", settings.CURRENT_SEASON)
    if current_season not in [s[0] for s in seasons]:
        current_season = settings.CURRENT_SEASON

    default = request.GET.get("default", "toty")
    if default not in ["toty", "soty"]:
        default = "toty"

    season_display = next(
        (label for value, label in seasons if value == current_season),
        current_season,
    )

    replay_api_url = f"{reverse('api:season_standings_replay')}?season={current_season}"

    return render(
        request,
        "core/replay.html",
        {
            "seasons": seasons,
            "current_season": current_season,
            "default": default,
            "season_display": season_display,
            "replay_api_url": replay_api_url,
        },
    )


def stats(request):
    """Statistics page with cached results for 1 hour"""
    cache_key = "stats_page_data"
    cached_data = cache.get(cache_key)

    if cached_data:
        return render(request, "core/stats.html", cached_data)

    current_season = settings.CURRENT_SEASON

    total_tournaments = Tournament.objects.count()
    total_debaters = Debater.objects.count()
    total_schools = School.objects.count()
    total_teams = Team.objects.count()
    total_team_results = TeamResult.objects.count()
    total_speaker_results = SpeakerResult.objects.count()

    avg_tournament_size = Tournament.objects.filter(num_teams__gt=0).aggregate(
        Avg('num_teams')
    )['num_teams__avg']

    schools_by_tournament_count = School.objects.annotate(
        tournament_count=Count('hosted_tournaments', distinct=True)
    ).filter(tournament_count__gt=0).order_by('-tournament_count')[:10]

    schools_by_coty_points = School.objects.annotate(
        total_coty_points=Sum('coty__points')
    ).filter(total_coty_points__isnull=False).order_by('-total_coty_points')[:10]

    multi_season_debaters = Debater.objects.exclude(
        first_season=F('latest_season')
    ).count()
    multi_season_percentage = (multi_season_debaters / total_debaters * 100) if total_debaters > 0 else 0

    teams_with_multiple_tournaments = Team.objects.annotate(
        tournament_count=Count('team_results__tournament', distinct=True)
    ).filter(tournament_count__gte=3).count()

    seasons = list(Tournament.objects.values_list('season', flat=True).distinct().order_by('season'))

    debaters_by_season = []
    for season in seasons:
        debater_count = Debater.objects.filter(
            Q(first_season__lte=season, latest_season__gte=season)
        ).count()
        debaters_by_season.append({
            'season': str(season),
            'count': debater_count
        })

    toty_points_by_season = list(TOTY.objects.values('season').annotate(
        total_points=Sum('points')
    ).order_by('season'))

    teams_by_season = []
    for season in seasons:
        team_count = Team.objects.filter(
            team_results__tournament__season=season
        ).distinct().count()
        teams_by_season.append({
            'season': str(season),
            'count': team_count
        })

    tournaments_by_season = list(Tournament.objects.values('season').annotate(
        tournament_count=Count('id'),
        avg_size=Avg('num_teams', filter=Q(num_teams__gt=0)),
        median_size=Count('id')
    ).order_by('season'))

    median_stats_by_season = []
    season_objs = Tournament.objects.values('season').distinct().order_by('season')
    for season_obj in season_objs:
        season = season_obj['season']
        tournament_sizes = list(Tournament.objects.filter(
            season=season, num_teams__gt=0
        ).values_list('num_teams', flat=True))

        novice_counts = list(Tournament.objects.filter(
            season=season, num_novice_debaters__gt=0
        ).values_list('num_novice_debaters', flat=True))

        median_stats_by_season.append({
            'season': str(season),
            'median_size': median(tournament_sizes) if tournament_sizes else 0,
            'median_novices': median(novice_counts) if novice_counts else 0
        })

    debaters_by_tournament_count = Debater.objects.annotate(
        tournament_count=Count('speaker_results__tournament', distinct=True)
    ).filter(tournament_count__gt=0).select_related('school').order_by('-tournament_count')[:10]

    teams_by_tournament_count = Team.objects.annotate(
        tournament_count=Count('team_results__tournament', distinct=True)
    ).filter(tournament_count__gt=0).prefetch_related(
        Prefetch('debaters', queryset=Debater.objects.select_related('school'))
    ).order_by('-tournament_count')[:10]

    # Debaters ranked by recorded rounds (number of videos they appear in)
    debaters_by_round_count = Debater.objects.annotate(
        round_count=(
            Count('pm_videos', distinct=True) +
            Count('lo_videos', distinct=True) +
            Count('mg_videos', distinct=True) +
            Count('mo_videos', distinct=True)
        )
    ).filter(round_count__gt=0).select_related('school').order_by('-round_count')[:10]

    context = {
        'total_tournaments': total_tournaments,
        'total_debaters': total_debaters,
        'total_schools': total_schools,
        'total_teams': total_teams,
        'total_team_results': total_team_results,
        'total_speaker_results': total_speaker_results,

        'current_season': current_season,
        'current_season_display': f"{current_season}-{str(int(current_season) + 1)[2:]}",

        'avg_tournament_size': round(avg_tournament_size, 1) if avg_tournament_size else 0,

        'multi_season_percentage': round(multi_season_percentage, 1),
        'teams_with_multiple_tournaments': teams_with_multiple_tournaments,

        'debaters_by_season': json.dumps(list(debaters_by_season)),
        'toty_points_by_season': json.dumps(list(toty_points_by_season)),
        'teams_by_season': json.dumps(list(teams_by_season)),
                'tournaments_by_season': json.dumps(list(tournaments_by_season)),
        'median_stats_by_season': json.dumps(list(median_stats_by_season)),

        'schools_by_tournament_count': schools_by_tournament_count,
        'schools_by_coty_points': schools_by_coty_points,
        'debaters_by_tournament_count': debaters_by_tournament_count,
        'teams_by_tournament_count': teams_by_tournament_count,
        'debaters_by_round_count': debaters_by_round_count,
    }

    # Cache for 1 hour (3600 seconds)
    cache.set(cache_key, context, 3600)

    return render(request, "core/stats.html", context)
