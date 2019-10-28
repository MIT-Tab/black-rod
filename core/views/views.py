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
    toty = TOTY.objects.filter(season=settings.CURRENT_SEASON).order_by('-points').all()[:10]
    coty = COTY.objects.filter(season=settings.CURRENT_SEASON).order_by('-points').all()[:10]
    soty = SOTY.objects.filter(season=settings.CURRENT_SEASON).order_by('-points').all()[:10]
    noty = NOTY.objects.filter(season=settings.CURRENT_SEASON).order_by('-points').all()[:10]

    
    return render(request,
                  'core/index.html',
                  locals())
