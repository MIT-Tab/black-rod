from django.db import models

from django.conf import settings

from core.models.debater import Debater
from core.models.standings.base import AbstractStanding


class TOTY(AbstractStanding):
    season = models.CharField(choices=settings.SEASONS,
                              default=settings.DEFAULT_SEASON,
                              max_length=16)

    debaters = models.ManyToManyField(Debater,
                                      related_name='toty')

    points = models.FloatField(default=-1)
