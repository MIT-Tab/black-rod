from django.core.management.base import BaseCommand
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.conf import settings
from tqdm import tqdm
import re

from core.models import School, Tournament, Team


class Command(BaseCommand):
    help = "Shortens tournament and team names by replacing school long names with short names"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview changes without saving',
        )

    def handle(self, *args, **kwargs):
        dry_run = kwargs.get('dry_run', False)
        
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN MODE - No changes will be saved'))
        
        # Process tournaments
        self.stdout.write('\nProcessing tournaments...')
        tournament_count = 0
        
        for tournament in tqdm(Tournament.objects.select_related('host').all()):
            old_name = tournament.name
            new_short_name = old_name
            
            # First, replace school long names with short names
            if tournament.host and tournament.host.name and tournament.host.short_name:
                if tournament.host.name in new_short_name:
                    new_short_name = new_short_name.replace(tournament.host.name, tournament.host.short_name)
            
            # Remove parenthetical sections like "(Elections)", "(Expansion)", etc.
            new_short_name = re.sub(r'\s*\([^)]*\)\s*', ' ', new_short_name).strip()
            
            if old_name != new_short_name or tournament.short_name != new_short_name:
                tournament.short_name = new_short_name
                if not dry_run:
                    tournament.save()
                
                if old_name != new_short_name:
                    self.stdout.write(
                        f'  Tournament: "{old_name}" -> "{new_short_name}"'
                    )
                    tournament_count += 1
        
        # Process teams
        self.stdout.write('\nProcessing teams...')
        team_count = 0
        
        for team in tqdm(Team.objects.prefetch_related('debaters__school').all()):
            old_name = team.name
            new_short_name = old_name
            
            # Get all schools from team's debaters
            schools = set()
            for debater in team.debaters.all():
                if debater.school:
                    schools.add(debater.school)
            
            # Replace each school's long name with short name
            for school in schools:
                if school.name and school.short_name and school.name in new_short_name:
                    new_short_name = new_short_name.replace(school.name, school.short_name)
            
            if old_name != new_short_name:
                team.short_name = new_short_name
                if not dry_run:
                    team.save()
                
                self.stdout.write(
                    f'  Team: "{old_name}" -> "{new_short_name}"'
                )
                team_count += 1
            else:
                # No replacement needed, just copy the name
                if team.short_name != team.name:
                    team.short_name = team.name
                    if not dry_run:
                        team.save()
        
        # Summary
        self.stdout.write('\n' + '='*60)
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN COMPLETE - No changes saved'))
        else:
            self.stdout.write(self.style.SUCCESS('COMPLETE - Changes saved'))
        
        self.stdout.write(f'Tournaments shortened: {tournament_count}')
        self.stdout.write(f'Teams shortened: {team_count}')
        
        # Clear all OTY caches for all seasons
        if not dry_run:
            self.stdout.write('\nClearing OTY caches...')
            cache_types = ['toty', 'soty', 'noty', 'coty', 'online_quals']
            seasons = settings.SEASONS
            
            for cache_type in cache_types:
                for season_tuple in seasons:
                    season = season_tuple[0]  # Extract season string from tuple
                    key = make_template_fragment_key(cache_type, [season])
                    cache.delete(key)
                    self.stdout.write(f'  Cleared {cache_type} cache for season {season}')
            
            self.stdout.write(self.style.SUCCESS('All OTY caches cleared successfully'))
