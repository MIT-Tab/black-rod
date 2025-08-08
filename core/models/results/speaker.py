from django.db import models

from core.models.debater import Debater
from core.models.tournament import Tournament


class SpeakerResult(models.Model):
    tournament = models.ForeignKey(Tournament,
                                   on_delete=models.CASCADE,
                                   related_name='speaker_results')

    debater = models.ForeignKey(Debater,
                                on_delete=models.CASCADE,
                                related_name='speaker_results')

    type_of_place = models.IntegerField(choices=Debater.STATUS,
                                        default=Debater.VARSITY)

    place = models.IntegerField(default=-1)

    tie = models.BooleanField(default=False)

    class Meta:
        unique_together = ('tournament', 'type_of_place', 'place')
