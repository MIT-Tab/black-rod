from django.conf import settings

from core.models import Team, Debater, TOTY, NOTY, COTY, SOTY

from core.utils.rankings import *

from tqdm import tqdm
from django.core.management.base import BaseCommand

class Command(BaseCommand):
        help = 'Updates TOTY, SOTY, NOTY rankings and qualification points'

        def add_arguments(self, parser):
            parser.add_argument(
                '--season',
                type=int,
                default=settings.CURRENT_SEASON,
                help='Season to update (defaults to current season)',
            )

        def handle(self, *args, **options):
            season = options['season']
            
            for team in tqdm(Team.objects.all()):
                self.stdout.write(f'Updating {team}')
                update_toty(team)

            for team in tqdm(Team.objects.all()):
                self.stdout.write(f'Updating {team}')    
                update_qual_points(team)

            for debater in tqdm(Debater.objects.all()):
                self.stdout.write(f'Updating {debater}')
                update_soty(debater)

            for debater in tqdm(Debater.objects.all()):
                self.stdout.write(f'Updating {debater}')    
                update_noty(debater)

            self.stdout.write('Ranking TOTY')
            redo_rankings(TOTY.objects.filter(season=season).all(), cache_type='toty')
            self.stdout.write('Ranking COTY')
            redo_rankings(COTY.objects.filter(season=season).all(), cache_type='coty')
            self.stdout.write('Ranking SOTY')
            redo_rankings(SOTY.objects.filter(season=season).all(), cache_type='soty')