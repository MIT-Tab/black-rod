from django.db import models

from core.models.debater import Debater
from core.models.tournament import Tournament


class TournamentAutoQual(models.Model):
    """An individual autoqual recipient, without a team or implied placement."""

    tournament = models.ForeignKey(
        Tournament, on_delete=models.CASCADE, related_name="autoqual_recipients"
    )
    debater = models.ForeignKey(
        Debater, on_delete=models.CASCADE, related_name="tournament_autoquals"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("tournament", "debater"), name="unique_tournament_autoqual_debater"
            )
        ]

    def __str__(self):
        return f"{self.debater} — {self.tournament}"
