from django.core.management.base import BaseCommand
from django.db.models import F, IntegerField, Max, Min, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce, Greatest, Least

from core.models import Debater, SpeakerResult, TeamResult


class Command(BaseCommand):
    help = "Seeds first_season and latest_season for debaters from participation data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--latest-only",
            action="store_true",
            help="Only update latest_season.",
        )
        parser.add_argument(
            "--first-only",
            action="store_true",
            help="Only update first_season.",
        )
        parser.add_argument(
            "--active-in-season",
            dest="active_in_season",
            help=(
                "Only update debaters with team or speaker results in this season "
                "(for example, 2020 for the 2020-2021 season)."
            ),
        )

    def handle(self, *args, **options):
        latest_only = options["latest_only"]
        first_only = options["first_only"]
        active_in_season = options["active_in_season"]

        team_min_sq = (
            TeamResult.objects.filter(team__debaters=OuterRef("pk"))
            .values("team__debaters")
            .annotate(v=Min("tournament__season"))
            .values("v")[:1]
        )
        team_max_sq = (
            TeamResult.objects.filter(team__debaters=OuterRef("pk"))
            .values("team__debaters")
            .annotate(v=Max("tournament__season"))
            .values("v")[:1]
        )
        speaker_min_sq = (
            SpeakerResult.objects.filter(debater=OuterRef("pk"))
            .values("debater")
            .annotate(v=Min("tournament__season"))
            .values("v")[:1]
        )
        speaker_max_sq = (
            SpeakerResult.objects.filter(debater=OuterRef("pk"))
            .values("debater")
            .annotate(v=Max("tournament__season"))
            .values("v")[:1]
        )

        queryset = Debater.objects.all()
        if active_in_season:
            queryset = queryset.filter(
                Q(teams__team_results__tournament__season=active_in_season)
                | Q(speaker_results__tournament__season=active_in_season)
            ).distinct()

        queryset = queryset.annotate(
            team_min_season=Subquery(team_min_sq, output_field=IntegerField()),
            team_max_season=Subquery(team_max_sq, output_field=IntegerField()),
            speaker_min_season=Subquery(speaker_min_sq, output_field=IntegerField()),
            speaker_max_season=Subquery(speaker_max_sq, output_field=IntegerField()),
        ).annotate(
            first_season_anno=Coalesce(
                Least("team_min_season", "speaker_min_season"),
                "team_min_season",
                "speaker_min_season",
            ),
            latest_season_anno=Coalesce(
                Greatest("team_max_season", "speaker_max_season"),
                "team_max_season",
                "speaker_max_season",
            ),
        )

        update_kwargs = {}
        if not latest_only:
            update_kwargs["first_season"] = F("first_season_anno")
        if not first_only:
            update_kwargs["latest_season"] = F("latest_season_anno")

        updated = queryset.update(**update_kwargs)

        updated_fields = []
        if "first_season" in update_kwargs:
            updated_fields.append("first_season")
        if "latest_season" in update_kwargs:
            updated_fields.append("latest_season")

        season_suffix = ""
        if active_in_season:
            season_suffix = f" for debaters active in season {active_in_season}"

        self.stdout.write(
            self.style.SUCCESS(
                "Debater season filters seeded successfully: "
                f"{', '.join(updated_fields)} updated for {updated} debaters{season_suffix}."
            )
        )
