from django.db import models

from core.models.debater import Debater
from core.models.team import Team
from core.models.tournament import Tournament


class TeamResult(models.Model):
    tournament = models.ForeignKey(
        Tournament, on_delete=models.CASCADE, related_name="team_results"
    )

    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="team_results"
    )

    type_of_place = models.IntegerField(choices=Debater.STATUS, default=Debater.VARSITY)

    place = models.IntegerField(default=-1)

    ghost_points = models.BooleanField(default=False)

    class Meta:
        unique_together = ("tournament", "type_of_place", "place")
