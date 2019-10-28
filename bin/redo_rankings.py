from django.conf import settings

from core.models import TOTY, NOTY, COTY, SOTY

from core.utils.rankings import *

redo_rankings(TOTY.objects.filter(season=settings.CURRENT_SEASON).all())
redo_rankings(NOTY.objects.filter(season=settings.CURRENT_SEASON).all())
redo_rankings(COTY.objects.filter(season=settings.CURRENT_SEASON).all())
redo_rankings(SOTY.objects.filter(season=settings.CURRENT_SEASON).all())
