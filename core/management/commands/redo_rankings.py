from django.conf import settings
from django.core.management.base import BaseCommand
from tqdm import tqdm

from core.models import Debater, Team
from core.utils.rankings import *


class Command(BaseCommand):
    help = "Recomputes rankings"

    def handle(self, *args, **kwargs):
        # for team in tqdm(Team.objects.all()):
        #     print ('Updating %s' % (team,))
        #     update_toty(team)
        qual_points = QualPoints.objects.filter(
            season=settings.CURRENT_SEASON, points=9
        )
        debaters = Debater.objects.filter(qual_points__in=qual_points).distinct()
        teams = Team.objects.filter(debaters__in=debaters).distinct()

        for team in tqdm(teams):
            print(f"Updating {team}")
            update_qual_points(team)

        # for debater in tqdm(Debater.objects.all()):
        #     print ('Updating %s' % (debater,))
        #     update_soty(debater)

        # print ('Ranking TOTY')
        # redo_rankings(TOTY.objects.filter(season=settings.CURRENT_SEASON).all(),cache_type='toty')
        # print ('Ranking COTY')
        # redo_rankings(COTY.objects.filter(season=settings.CURRENT_SEASON).all(), cache_type='coty')
        # print ('Ranking SOTY')
        # redo_rankings(SOTY.objects.filter(season=settings.CURRENT_SEASON).all(), cache_type='soty')
