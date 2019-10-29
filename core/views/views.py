from django.shortcuts import render

from django.conf import settings

from core.models import (
    TOTY,
    SOTY,
    NOTY,
    COTY
)

# Create your views here.


def index(request):
    seasons = settings.SEASONS
    current_season = request.GET.get('season', settings.CURRENT_SEASON)

    toty = TOTY.objects.filter(season=current_season).order_by('-points').all()
    coty = COTY.objects.filter(season=current_season).order_by('-points').all()
    soty = SOTY.objects.filter(season=current_season).order_by('-points').all()
    noty = NOTY.objects.filter(season=current_season).order_by('-points').all()

    
    return render(request,
                  'core/index.html',
                  locals())
