from django.conf import settings

from core.models import Team, Debater, TOTY, NOTY, COTY, SOTY

from core.utils.rankings import *

from tqdm import tqdm

season = settings.CURRENT_SEASON

for team in tqdm(Team.objects.all()):
    print ('Updating %s' % (team,))
    update_toty(team)

for team in tqdm(Team.objects.all()):
    print ('Updating %s' % (team,))    
    update_qual_points(team)

for debater in tqdm(Debater.objects.all()):
    print ('Updating %s' % (debater,))
    update_soty(debater)

for debater in tqdm(Debater.objects.all()):
    print ('Updating %s' % (debater,))    
    update_noty(debater)


print ('Ranking TOTY')
redo_rankings(TOTY.objects.filter(season=season).all(),cache_type='toty')
print ('Ranking COTY')
redo_rankings(COTY.objects.filter(season=season).all(), cache_type='coty')
print ('Ranking SOTY')
redo_rankings(SOTY.objects.filter(season=season).all(), cache_type='soty')
