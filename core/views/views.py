from django.shortcuts import render

from django.conf import settings

from core.models import (
    TOTY,
    SOTY,
    NOTY,
    COTY,
    OnlineQUAL
)

# Create your views here.


def index(request):
    seasons = settings.SEASONS
    current_season = request.GET.get('season', settings.CURRENT_SEASON)

    default = request.GET.get('default', 'toty')

    toty = TOTY.objects.filter(season=current_season).order_by('-points').all()
    coty = COTY.objects.filter(season=current_season).order_by('-points').all()
    soty = SOTY.objects.filter(season=current_season).order_by('-points').all()
    noty = NOTY.objects.filter(season=current_season).order_by('-points').all()

    if current_season in settings.ONLINE_SEASONS:
        using_online_quals = True
        online_quals = OnlineQUAL.objects.filter(
            season=current_season
        ).order_by('-points').all()

        online_seasons = [
            (season[0], season[1]) for season in seasons if season[0] in settings.ONLINE_SEASONS
        ]

        online_qual_bar = settings.ONLINE_QUAL_BAR

    return render(request,
                  'core/index.html',
                  locals())
