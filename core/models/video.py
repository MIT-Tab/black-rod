from django.db import models

from core.models.debater import Debater
from core.models.team import Team

from core.models.tournament import Tournament


class Video(models.Model):
    debaters = models.ManyToManyField(Debater)

    teams = models.ManyToManyField(Team)

    tournament = models.ForeignKey(Tournament,
                                   on_delete=models.CASCADE,
                                   related_name='videos')
