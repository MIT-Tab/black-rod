from django.db import models

from django.conf import settings

from apda.models.debater import Debater


class TOTY(models.Model):
    season = models.CharField(choices=settings.SEASONS,
                              default=settings.DEFAULT_SEASON)

    debaters = models.ManyToManyField(Debater,
                                      related_name='toty')

    points = models.FloatField(default=-1)

    class Meta:
        unique_together = ('season', 'debaters')
