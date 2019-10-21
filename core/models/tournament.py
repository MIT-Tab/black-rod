from django.db import models

from django.conf import settings


class Tournament(models.Model):
    name = models.CharField(max_length=128,
                            blank=False)

    num_rounds = models.IntegerField(default=5)

    school_year = models.CharField(choices=settings.SCHOOL_YEARS,
                                   default=settings.DEFAULT_SCHOOL_YEAR)

    num_teams = models.IntegerField(null=False)
    num_novice_teams = models.IntegerField(null=False)

    num_debaters = models.IntegerField(null=False)
    num_novice_debaters = models.IntegerField(null=False)

    noty = models.BooleanField(default=True)
    soty = models.BooleanField(default=True)

    qual_bar = models.IntegerField(default=0)
