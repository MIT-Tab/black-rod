from django.db import models

from django.conf import settings

from apda.models.school import School


class COTY(models.Model):
    season = models.CharField(choices=settings.SEASONS,
                              default=settings.DEFAULT_SEASON)

    school = models.ForeignKey(School,
                               related_name='coty')

    points = models.FloatField(default=-1)

    class Meta:
        unique_together = ('season', 'school')
