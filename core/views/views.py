from django.conf import settings
from django.shortcuts import render

from core.models import COTY, NOTY, SOTY, TOTY, OnlineQUAL


def index(request):
    seasons = settings.SEASONS
    current_season = request.GET.get("season", settings.CURRENT_SEASON)
    default = request.GET.get("default", "toty")

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
