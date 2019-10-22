from djang.db import models

from apda.models.debater import Debater
from apda.models.tournament import Tournament


class SpeakerResult(models.Model):
    tournament = models.ForeignKey(Tournament,
                                   related_name='speaker_results')

    debater = models.ForeignKey(Debater,
                                on_delete=models.CASCADE)

    type_of_place = models.IntegerFIeld(choices=Debater.STATUS,
                                        default=Debater.VARSITY)

    place = models.IntegerField(default=-1)

    class Meta:
        unique_together = ('tournament', 'type_of_place', 'place')
