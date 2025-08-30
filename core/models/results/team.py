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

    # -1 -> did not place
    place = models.IntegerField(default=-1)

    ghost_points = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tournament", "type_of_place", "place"],
                condition=~models.Q(place=-1), # Allows many results with place -1
                name="unique_teamresult_place_when_not_minus_one"
            )
        ]
