from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import QualPoints
from core.models.standings.qual import QUAL


class Command(BaseCommand):
    help = (
        "Backfill missing historical point quals from QualPoints using "
        "settings.HISTORICAL_QUAL_BARS. Only creates missing rows."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            dest="all_seasons",
            help=(
                "Reconstruct every historical season configured in "
                "HISTORICAL_QUAL_BARS (excludes current season)."
            ),
        )
        parser.add_argument(
            "--season",
            action="append",
            dest="seasons",
            default=[],
            help="Season to reconstruct (repeat flag for multiple seasons).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without writing to the database.",
        )

    @staticmethod
    def _is_historical_season(season):
        try:
            return int(season) < int(settings.CURRENT_SEASON)
        except (TypeError, ValueError):
            return False

    def _target_seasons(self, requested, all_seasons):
        bars = getattr(settings, "HISTORICAL_QUAL_BARS", {})
        if all_seasons:
            return [
                season
                for season in sorted(bars.keys())
                if self._is_historical_season(season)
            ]
        if requested:
            return [str(season) for season in requested]
        return sorted(bars.keys())

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        target_seasons = self._target_seasons(
            options["seasons"], options["all_seasons"]
        )
        historical_bars = getattr(settings, "HISTORICAL_QUAL_BARS", {})

        if not target_seasons:
            self.stdout.write("No seasons selected. Nothing to do.")
            return

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN: no rows will be written"))

        attempted_total = 0
        for season in target_seasons:
            if season in settings.ONLINE_SEASONS:
                self.stdout.write(f"Skipping online season {season}")
                continue

            if not self._is_historical_season(season):
                self.stdout.write(f"Skipping non-historical season {season}")
                continue

            if season not in historical_bars:
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipping {season}: no HISTORICAL_QUAL_BARS entry"
                    )
                )
                continue

            qual_bar = float(historical_bars[season])
            candidate_ids = set(
                QualPoints.objects.filter(
                    season=season,
                    points__gte=qual_bar,
                    debater__school__included_in_oty=True,
                )
                .values_list("debater_id", flat=True)
            )
            existing_qualified_ids = set(
                QUAL.objects.filter(
                    season=season, debater_id__in=candidate_ids
                ).values_list("debater_id", flat=True)
            )
            missing_ids = sorted(candidate_ids - existing_qualified_ids)
            attempted_total += len(missing_ids)

            self.stdout.write(
                f"Season {season}: bar={qual_bar}, candidates={len(candidate_ids)}, "
                f"already_qualified={len(existing_qualified_ids)}, "
                f"missing={len(missing_ids)}"
            )

            if not missing_ids or dry_run:
                continue

            to_create = [
                QUAL(
                    season=season,
                    debater_id=debater_id,
                    qual_type=QUAL.POINTS,
                    place=-1,
                    points=-1,
                    tied=False,
                    tournament_id=None,
                )
                for debater_id in missing_ids
            ]
            with transaction.atomic():
                QUAL.objects.bulk_create(to_create, ignore_conflicts=True, batch_size=1000)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run complete. Would attempt to create up to {attempted_total} rows."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Reconstruction complete. Attempted to create {attempted_total} rows."
            )
        )
