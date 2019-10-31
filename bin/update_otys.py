from django.conf import settings

from core.models import Team, Debater, TOTY, NOTY, COTY, SOTY

from core.utils.rankings import *

for team in Team.objects.all():
    update_toty(team)
    update_qual_points(team)

for debater in Debater.objects.all():
    update_soty(debater)
    update_noty(debater)


redo_rankings(TOTY.objects.filter(season=settings.CURRENT_SEASON, cache_type='toty').all())
redo_rankings(NOTY.objects.filter(season=settings.CURRENT_SEASON, cache_type='noty').all())
redo_rankings(COTY.objects.filter(season=settings.CURRENT_SEASON, cache_type='coty').all())
redo_rankings(SOTY.objects.filter(season=settings.CURRENT_SEASON, cache_type='soty').all())
