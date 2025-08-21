from django.conf import settings
from django.core.management.base import BaseCommand
from tqdm import tqdm

from core.models import Debater, Team, QualPoints
from core.models.standings.qual import QualBar, QUAL
from core.utils.rankings import *


class Command(BaseCommand):
    help = "Creates seed data for qual bars"

    def handle(self, *args, **kwargs):
        for season, _ in settings.SEASONS:
            if season in settings.ONLINE_SEASONS:
                continue
            if not QualBar.objects.filter(season=season).exists():
                print(f"Creating QualBar for season: {season}")
                min_points_qualled = float('inf')
                for qual in QUAL.objects.filter(season=season, qual_type=QUAL.POINTS).prefetch_related('debater__qual_points').all():
                    if qual.debater.qual_points.filter(season=season).exists():
                        points = qual.debater.qual_points.get(season=season).points
                        if points < min_points_qualled:
                            min_points_qualled = points
                print(f"Minimum points qualified for season {season}: {min_points_qualled}")
                if 0 < min_points_qualled < float('inf'):
                    QualBar.objects.create(season=season, points=min_points_qualled)