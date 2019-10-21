from django.db import models

from apda.models.debater import Debater
from apda.models.tournament import Tournament


class TeamResult(models.Model):
    tournament = models.ForeignKey(Tournament,
                                   related_name='team_results')
    
    debaters = models.ManyToManyField(Debater)

    type_of_place = models.IntegerField(choices=Debater.STATUS,
                                        default=Debater.VARSITY)

    place = models.IntegerField(default=-1)
