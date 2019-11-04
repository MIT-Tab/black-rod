from django.conf import settings

from core.models import Team, Debater, TOTY, NOTY, COTY, SOTY

from core.utils.rankings import *

for team in Team.objects.all():
    print ('Updating %s' % (team,))
    update_toty(team)

for team in Team.objects.all():
    print ('Updating %s' % (team,))    
    update_qual_points(team)

for debater in Debater.objects.all():
    print ('Updating %s' % (debater,))
    update_soty(debater)

for debater in Debater.objects.all():
    print ('Updating %s' % (debater,))    
    update_noty(debater)


print ('Ranking TOTY')
redo_rankings(TOTY.objects.filter(season=settings.CURRENT_SEASON).all(),cache_type='toty')
print ('Ranking NOTY')
redo_rankings(NOTY.objects.filter(season=settings.CURRENT_SEASON).all(), cache_type='noty')
print ('Ranking COTY')
redo_rankings(COTY.objects.filter(season=settings.CURRENT_SEASON).all(), cache_type='coty')
print ('Ranking SOTY')
redo_rankings(SOTY.objects.filter(season=settings.CURRENT_SEASON).all(), cache_type='soty')
