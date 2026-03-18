from django.core.management.base import BaseCommand

from core.models import Team


class Command(BaseCommand):
    help = (
        "Replace synthetic team name and short_name values with native "
        "school-plus-initials labels."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview synthetic team renames without saving them.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        updated = 0
        skipped = 0

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN MODE - No changes will be saved"))

        queryset = Team.objects.filter(synthetic=True).prefetch_related("debaters__school").order_by("id")

        for team in queryset:
            native_name = team.build_native_name()
            native_short_name = team.build_native_short_name()

            if not native_name:
                skipped += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipping synthetic team {team.id}: no debaters available for native naming."
                    )
                )
                continue

            if team.name == native_name and team.short_name == native_short_name:
                continue

            self.stdout.write(
                f'Team {team.id}: "{team.name}" / "{team.short_name}" -> '
                f'"{native_name}" / "{native_short_name}"'
            )
            updated += 1

            if dry_run:
                continue

            team.name = native_name
            team.short_name = native_short_name
            team.save(update_fields=["name", "short_name"])

        summary_style = self.style.WARNING if dry_run else self.style.SUCCESS
        self.stdout.write(
            summary_style(
                f"Processed {queryset.count()} synthetic teams; updated {updated}, skipped {skipped}."
            )
        )
