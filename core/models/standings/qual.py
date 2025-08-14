from django.db import models

from core.models.debater import Debater
from core.models.standings.base import AbstractStanding
from core.models.tournament import Tournament


class QUAL(AbstractStanding):
    debater = models.ForeignKey(Debater, on_delete=models.CASCADE, related_name="quals")

    POINTS = 0
    BRANDEIS = 1
    YALE = 2
    NORTHAMS = 3
    EXPANSION = 4
    WORLDS = 5
    NAUDC = 6
    PROAMS = 7
    NATIONALS = 8
    NOVICE = 9

    QUAL_TYPES = (
        (POINTS, "Points"),
        (BRANDEIS, "Brandeis IV"),
        (YALE, "Yale IV"),
        (NORTHAMS, "NorthAms"),
        (EXPANSION, "Expansion"),
        (WORLDS, "Worlds"),
        (NAUDC, "NAUDC"),
        (PROAMS, "ProAms"),
        (NATIONALS, "Nationals"),
        (NOVICE, "Novice"),
    )

    qual_type = models.IntegerField(choices=QUAL_TYPES, default=POINTS)

    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="quals",
        blank=True,
        null=True,
    )

    class Meta:
        unique_together = ("season", "debater", "qual_type")
