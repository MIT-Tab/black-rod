from django.core.management.base import BaseCommand
from django.conf import settings
from core.models import QUAL, QualPoints
from core.utils.rankings import *
from tqdm import tqdm
from django.conf import settings

class Command(BaseCommand):
    help = 'Recomputes qual bars'

    def handle(self, *args, **kwargs):
        for season, season_display in settings.SEASONS:
            if int(season) < 2019 or int(season) > 2024:
                continue
            min_pts_qualled = 100000
            max_pts_unqualled = 0
            for qual in QualPoints.objects.filter(season=season).all():
                qual_points = qual.points
                if QUAL.objects.filter(season=season, debater=qual.debater, qual_type=QUAL.POINTS).exists():
                    min_pts_qualled = min(min_pts_qualled, qual_points)
                else:
                    max_pts_unqualled = max(max_pts_unqualled, qual_points)
            print(f'Season {season} - Min Qualled: {min_pts_qualled}, Max Unqualled: {max_pts_unqualled}')
