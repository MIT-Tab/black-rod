from django.core.management.base import BaseCommand
from django.db.models import Count
from core.models.debater import Debater


class Command(BaseCommand):
    help = "Finds and deletes duplicate debaters that have no results"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be deleted without actually deleting',
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Required to actually delete. Must run with --dry-run first.',
        )
        parser.add_argument(
            '--min-id',
            type=int,
            required=True,
            help='Only check debaters with ID greater than or equal to this value',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        confirm = options['confirm']
        min_id = options['min_id']
        
        # Safety check: cannot use both --dry-run and --confirm
        if dry_run and confirm:
            self.stdout.write(
                self.style.ERROR(
                    "Error: Cannot use both --dry-run and --confirm together. "
                    "Use --dry-run first to see what will be deleted, "
                    "then run again with --confirm to actually delete."
                )
            )
            return
        
        # Safety check: must specify either --dry-run or --confirm
        if not dry_run and not confirm:
            self.stdout.write(
                self.style.ERROR(
                    "Error: Must specify either --dry-run or --confirm.\n"
                    "First run with --dry-run to see what will be deleted:\n"
                    f"  python manage.py clean_duplicate_debaters --min-id {min_id} --dry-run\n"
                    "Then run with --confirm to actually delete:\n"
                    f"  python manage.py clean_duplicate_debaters --min-id {min_id} --confirm"
                )
            )
            return
        
        self.stdout.write(f"Checking for duplicate debaters with ID >= {min_id}")
        debaters_to_check = Debater.objects.filter(id__gte=min_id)

        # Find groups of debaters with same first_name, last_name, and school
        duplicates_info = (
            debaters_to_check
            .values('first_name', 'last_name', 'school')
            .annotate(count=Count('id'))
            .filter(count__gt=1)
        )

        total_deleted = 0
        debaters_to_delete = []

        for dup in duplicates_info:
            # Get all debaters in this duplicate group
            duplicate_group = debaters_to_check.filter(
                first_name=dup['first_name'],
                last_name=dup['last_name'],
                school=dup['school']
            )

            # Separate those with results from those without
            debaters_with_results = []
            debaters_without_results = []

            for debater in duplicate_group:
                has_results = (
                    debater.teams.exists() or 
                    debater.speaker_results.exists()
                )
                
                if has_results:
                    debaters_with_results.append(debater)
                else:
                    debaters_without_results.append(debater)

            # Only process if we have at least one with results and one without
            if debaters_with_results and debaters_without_results:
                for debater in debaters_without_results:
                    debaters_to_delete.append(debater.id)
                    self.stdout.write(
                        f"  Found duplicate: {debater.name} (ID: {debater.id}) "
                        f"from {debater.school.name if debater.school else 'No School'} - "
                        f"NO RESULTS"
                    )
                
                for debater in debaters_with_results:
                    self.stdout.write(
                        f"  Keeping: {debater.name} (ID: {debater.id}) "
                        f"from {debater.school.name if debater.school else 'No School'} - "
                        f"HAS RESULTS"
                    )
                
                total_deleted += len(debaters_without_results)

        if debaters_to_delete:
            if dry_run:
                self.stdout.write(
                    self.style.WARNING(
                        f"\nDRY RUN: Would delete {total_deleted} duplicate debater(s)"
                    )
                )
            else:
                # Batch delete
                deleted_count, _ = Debater.objects.filter(id__in=debaters_to_delete).delete()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"\nSuccessfully deleted {deleted_count} duplicate debater(s)"
                    )
                )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nNo duplicate debaters found with ID >= {min_id}"
                )
            )
