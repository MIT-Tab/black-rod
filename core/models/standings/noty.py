from django.db import models

from core.models.debater import Debater
from core.models.standings.base import AbstractStanding
from core.models.tournament import Tournament


class NOTY(AbstractStanding):
    debater = models.ForeignKey(Debater, on_delete=models.CASCADE, related_name="noty")

    class Meta:
        unique_together = ("season", "debater")
        ordering = ("place",)

    marker_one = models.FloatField(default=0)
    marker_two = models.FloatField(default=0)
    marker_three = models.FloatField(default=0)
    marker_four = models.FloatField(default=0)
    marker_five = models.FloatField(default=0)
    marker_six = models.FloatField(default=0)

    tournament_one = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="noty_tournament_one",
        null=True,
    )
    tournament_two = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="noty_tournament_two",
        null=True,
    )
    tournament_three = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="noty_tournament_three",
        null=True,
    )
    tournament_four = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="noty_tournament_four",
        null=True,
    )
    tournament_five = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="noty_tournament_five",
        null=True,
    )
    tournament_six = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="noty_tournament_six",
        null=True,
    )
