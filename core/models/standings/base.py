from django.db import models
from django.conf import settings


class StandingsManager(models.Manager):
    def get_queryset(self):
        return self._queryset_class(model=self.model,
                                    using=self._db,
                                    hints=self._hints)

    def get_for_season(self, season=None):
        if not season:
            season = settings.CURRENT_SEASON

        return self.get_queryset().filter(
            season=season
        )


class AllSeasonManager(models.Manager):
    pass
        

class AbstractStanding(models.Model):
    season = models.CharField(choices=settings.SEASONS,
                              default=settings.DEFAULT_SEASON,
                              max_length=16)

    points = models.FloatField(default=-1)

    class Meta:
        abstract = True

    objects = StandingsManager()
    all_objects = AllSeasonManager()
