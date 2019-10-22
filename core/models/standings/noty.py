from django.db import models

from django.conf import settings

from apda.models.debater import Debater


class NOTY(models.Model):
    season = models.CharField(choices=settings.SEASONS,
                              default=settings.DEFAULT_SEASON)

    debater = models.ForeignKey(Debater,
                                on_delete=models.CASCADE,
                                related_name='noty')

    points = models.FloatField(default=-1)

    class Meta:
        unique_together = ('season', 'debater')
