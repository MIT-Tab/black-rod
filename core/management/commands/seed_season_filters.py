from django.core.management.base import BaseCommand
from django.db.models import Min, Max, Subquery, OuterRef, IntegerField, F
from django.db.models.functions import Coalesce, Least, Greatest
from core.models import Debater, TeamResult, SpeakerResult

class Command(BaseCommand):
    help = 'Seeds first_season and latest_season for all debaters.'

    def handle(self, *args, **options):
        team_min_sq = (
            TeamResult.objects
            .filter(team__debaters=OuterRef('pk'))
            .values('team__debaters')
            .annotate(v=Min('tournament__season'))
            .values('v')[:1]
        )
        team_max_sq = (
            TeamResult.objects
            .filter(team__debaters=OuterRef('pk'))
            .values('team__debaters')
            .annotate(v=Max('tournament__season'))
            .values('v')[:1]
        )
        speaker_min_sq = (
            SpeakerResult.objects
            .filter(debater=OuterRef('pk'))
            .annotate(v=Min('tournament__season'))
            .values('v')[:1]
        )
        speaker_max_sq = (
            SpeakerResult.objects
            .filter(debater=OuterRef('pk'))
            .annotate(v=Max('tournament__season'))
            .values('v')[:1]
        )
        (
            Debater.objects
            .annotate(
                team_min_season=Subquery(team_min_sq, output_field=IntegerField()),
                team_max_season=Subquery(team_max_sq, output_field=IntegerField()),
                speaker_min_season=Subquery(speaker_min_sq, output_field=IntegerField()),
                speaker_max_season=Subquery(speaker_max_sq, output_field=IntegerField()),
            )
            .annotate(
                first_season_anno=Coalesce(
                    Least('team_min_season', 'speaker_min_season'),
                    'team_min_season',
                    'speaker_min_season',
                ),
                latest_season_anno=Coalesce(
                    Greatest('team_max_season', 'speaker_max_season'),
                    'team_max_season',
                    'speaker_max_season',
                ),
            )
            .update(
                first_season=F('first_season_anno'),
                latest_season=F('latest_season_anno'),
            )
        )

        self.stdout.write(self.style.SUCCESS('Debater season filters seeded successfully.'))
