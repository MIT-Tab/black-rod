from django.db import models

from django.conf import settings

from core.utils.points import (
    team_points_for_size,
    speaker_points_for_size
)


class Tournament(models.Model):
    name = models.CharField(max_length=128,
                            blank=False)

    num_rounds = models.IntegerField(default=5)

    season = models.CharField(choices=settings.SEASONS,
                              default=settings.DEFAULT_SEASON)

    num_teams = models.IntegerField(null=False)
    num_novice_teams = models.IntegerField(null=False)

    num_debaters = models.IntegerField(null=False)
    num_novice_debaters = models.IntegerField(null=False)

    qual = models.BooleanField(default=True)
    noty = models.BooleanField(default=True)
    soty = models.BooleanField(default=True)
    toty = models.BooleanField(default=True)

    qual_bar = models.IntegerField(default=0)


    def get_qualled(self, place):
        if self.qual_bar < 1:
            return False

        return place <= self.qual_bar
        
        
    def get_qual_points(self, place):
        if not self.qual:
            return 0

        return team_points_for_size(self.num_teams,
                                    place)


    def get_toty_points(self, place):
        if not self.toty:
            return 0

        return self.get_qual_points(place)


    def get_soty_points(self, place):
        if not self.soty:
            return 0

        return speaker_points_for_size(self.num_debaters,
                                       place)


    def get_noty_points(self, place):
        if not self.noty:
            return 0

        return speaker_points_for_size(self.num_novice_debaters,
                                       place)
