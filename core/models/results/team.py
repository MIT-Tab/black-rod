from django.db import models

from core.models.team import Team
from core.models.tournament import Tournament


class TeamResult(models.Model):
    tournament = models.ForeignKey(
        Tournament, on_delete=models.CASCADE, related_name="team_results"
    )

    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="team_results"
    )

    NOVICE = 0
    VARSITY = 1
    STATUS = ((VARSITY, "Varsity"), (NOVICE, "Novice"))
    type_of_place = models.IntegerField(choices=STATUS, default=VARSITY)

    # -1 -> did not place
    place = models.IntegerField(default=-1)

    ghost_points = models.BooleanField(default=False)
    counts_for_points = models.BooleanField(default=True)
    hidden_from_debaters = models.ManyToManyField(
        "core.Debater",
        blank=True,
        related_name="hidden_team_results",
        help_text=(
            "Hide this result on the selected debaters' profiles. Other members keep "
            "the result without team or partner details. The team profile also hides it."
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tournament", "type_of_place", "place"],
                condition=~models.Q(place=-1), # Allows many results with place -1
                name="unique_teamresult_place_when_not_minus_one"
            )
        ]
