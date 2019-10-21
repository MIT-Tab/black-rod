from djang.db import models

from apda.models.debater import Debater
from apda.models.tournament import Tournament


class SpeakerResult(models.Model):
    tournament = models.ForeignKey(Tournament,
                                   related_name='speaker_results')

    debater = models.ForeignKey(Debater,
                                on_delete=models.CASCADE)
