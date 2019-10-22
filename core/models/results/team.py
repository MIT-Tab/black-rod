from django.db import models

from core.models.debater import Debater
from core.models.tournament import Tournament


class TeamResult(models.Model):
    tournament = models.ForeignKey(Tournament,
                                   on_delete=models.CASCADE,
                                   related_name='team_results')
    
    debaters = models.ManyToManyField(Debater)

    type_of_place = models.IntegerField(choices=Debater.STATUS,
                                        default=Debater.VARSITY)

    place = models.IntegerField(default=-1)

    class Meta:
        unique_together = ('tournament', 'type_of_place', 'place')
