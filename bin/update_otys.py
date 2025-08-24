from django.conf import settings
from tqdm import tqdm

from core.models import COTY, NOTY, SOTY, TOTY, Debater, Team
from core.utils.rankings import *

for team in tqdm(Team.objects.all()):
    print(f"Updating {team}")
    update_toty(team)

for team in tqdm(Team.objects.all()):
    print(f"Updating {team}")
    update_qual_points(team)

for debater in tqdm(Debater.objects.all()):
    print(f"Updating {debater}")
    update_soty(debater)

for debater in tqdm(Debater.objects.all()):
    print(f"Updating {debater}")
    update_noty(debater)


print("Ranking TOTY")
redo_rankings(
    TOTY.objects.filter(season=settings.CURRENT_SEASON).all(), cache_type="toty"
)
print("Ranking NOTY")
redo_rankings(
    NOTY.objects.filter(season=settings.CURRENT_SEASON).all(), cache_type="noty"
)
print("Ranking COTY")
redo_rankings(
    COTY.objects.filter(season=settings.CURRENT_SEASON).all(), cache_type="coty"
)
print("Ranking SOTY")
redo_rankings(
    SOTY.objects.filter(season=settings.CURRENT_SEASON).all(), cache_type="soty"
)
