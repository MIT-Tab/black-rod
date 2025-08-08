from django.conf import settings

from core.models import TOTY, NOTY, COTY, SOTY

from core.utils.rankings import *

for season, season_display in settings.SEASONS:
    redo_rankings(TOTY.objects.filter(season=settings.CURRENT_SEASON).all(), season=season, cache_type='toty')
    #redo_rankings(NOTY.objects.filter(season=settings.CURRENT_SEASON).all(), season=season, cache_type='noty')
    redo_rankings(COTY.objects.filter(season=settings.CURRENT_SEASON).all(), season=season, cache_type='coty')
    redo_rankings(SOTY.objects.filter(season=settings.CURRENT_SEASON).all(), season=season, cache_type='soty')
