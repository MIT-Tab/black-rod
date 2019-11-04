from django.conf import settings

from core.models import Team, Debater, TOTY, NOTY, COTY, SOTY

from core.utils.rankings import *

for team in Team.objects.all():
    print ('Updating %s' % (team,))
    update_toty(team)
    update_qual_points(team)

for debater in Debater.objects.all():
    print ('Updating %s' % (debater,))
    update_soty(debater)
    update_noty(debater)


print ('Ranking TOTY')
redo_rankings(TOTY.objects.filter(season=settings.CURRENT_SEASON, cache_type='toty').all())
print ('Ranking NOTY')
redo_rankings(NOTY.objects.filter(season=settings.CURRENT_SEASON, cache_type='noty').all())
print ('Ranking COTY')
redo_rankings(COTY.objects.filter(season=settings.CURRENT_SEASON, cache_type='coty').all())
print ('Ranking SOTY')
redo_rankings(SOTY.objects.filter(season=settings.CURRENT_SEASON, cache_type='soty').all())
