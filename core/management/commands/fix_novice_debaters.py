from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from core.models.debater import Debater


class Command(BaseCommand):
    help = "Finds debaters with first_season=None who only competed this season and sets them as novices"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be updated without actually updating',
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Required to actually update. Must run with --dry-run first.',
        )
        parser.add_argument(
            '--season',
            type=str,
            help=f'Season to check (defaults to current season: {settings.CURRENT_SEASON})',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        confirm = options['confirm']
        season = options.get('season') or settings.CURRENT_SEASON
        
        # Safety check: cannot use both --dry-run and --confirm
        if dry_run and confirm:
            self.stdout.write(
                self.style.ERROR(
                    "Error: Cannot use both --dry-run and --confirm together. "
                    "Use --dry-run first to see what will be updated, "
                    "then run again with --confirm to actually update."
                )
            )
            return
        
        # Safety check: must specify either --dry-run or --confirm
        if not dry_run and not confirm:
            self.stdout.write(
                self.style.ERROR(
                    "Error: Must specify either --dry-run or --confirm.\n"
                    "First run with --dry-run to see what will be updated:\n"
                    "  python manage.py fix_novice_debaters --dry-run\n"
                    "Then run with --confirm to actually update:\n"
                    "  python manage.py fix_novice_debaters --confirm"
                )
            )
            return
        
        self.stdout.write(f"Checking for debaters with first_season=None who only competed in {season}")
        
        # Find debaters with first_season=None
        debaters_without_first_season = Debater.objects.filter(
            Q(first_season__isnull=True) | Q(first_season='')
        )
        
        debaters_to_update = []
        
        for debater in debaters_without_first_season:
            # Check tournaments this debater competed in via their teams' results
            team_result_seasons = set()
            for team in debater.teams.all():
                seasons = team.team_results.values_list('tournament__season', flat=True)
                team_result_seasons.update(seasons)
            
            # Check tournaments this debater competed in via speaker results
            speaker_result_seasons = set(
                debater.speaker_results.values_list('tournament__season', flat=True)
            )
            
            # Combine all seasons
            all_seasons = team_result_seasons.union(speaker_result_seasons)
            
            # Filter out None values
            all_seasons = {s for s in all_seasons if s}
            
            # If debater only competed in the specified season
            if all_seasons == {season}:
                debaters_to_update.append(debater)
                self.stdout.write(
                    f"  Found: {debater.name} (ID: {debater.id}) "
                    f"from {debater.school.name if debater.school else 'No School'} - "
                    f"only competed in {season}"
                )
        
        if debaters_to_update:
            if dry_run:
                self.stdout.write(
                    self.style.WARNING(
                        f"\nDRY RUN: Would update {len(debaters_to_update)} debater(s) to:\n"
                        f"  - first_season: {season}\n"
                        f"  - latest_season: {season}\n"
                        f"  - status: NOVICE ({Debater.NOVICE})"
                    )
                )
            else:
                # Batch update
                updated_count = 0
                for debater in debaters_to_update:
                    debater.first_season = season
                    debater.latest_season = season
                    debater.status = Debater.NOVICE
                    debater.save()
                    updated_count += 1
                
                self.stdout.write(
                    self.style.SUCCESS(
                        f"\nSuccessfully updated {updated_count} debater(s) to:\n"
                        f"  - first_season: {season}\n"
                        f"  - latest_season: {season}\n"
                        f"  - status: NOVICE ({Debater.NOVICE})"
                    )
                )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nNo debaters found with first_season=None who only competed in {season}"
                )
            )
