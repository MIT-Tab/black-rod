from django.db import models

from django.conf import settings

from core.models.school import School


class COTY(models.Model):
    season = models.CharField(choices=settings.SEASONS,
                              default=settings.DEFAULT_SEASON,
                              max_length=16)

    school = models.ForeignKey(School,
                               on_delete=models.CASCADE,
                               related_name='coty')

    points = models.FloatField(default=-1)

    class Meta:
        unique_together = ('season', 'school')
