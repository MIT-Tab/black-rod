from django.db import models
from django.shortcuts import reverse
from django.conf import settings

from core.utils.points import (
    team_points_for_size,
    speaker_points_for_size
)
from core.models.school import School


class Tournament(models.Model):
    name = models.CharField(max_length=128,
                            blank=False)

    num_rounds = models.IntegerField(default=5,
                                     verbose_name='Rounds')

    host = models.ForeignKey(School,
                             on_delete=models.SET_NULL,
                             related_name='hosted_tournaments',
                             blank=True,
                             null=True)

    season = models.CharField(choices=settings.SEASONS,
                              default=settings.DEFAULT_SEASON,
                              max_length=16)

    num_teams = models.IntegerField(null=False,
                                    verbose_name='Teams')
    num_novice_teams = models.IntegerField(null=False,
                                           verbose_name='Novice Teams',
                                           default=-1)

    num_debaters = models.IntegerField(null=False,
                                       verbose_name='Debaters',
                                       default=-1)
    num_novice_debaters = models.IntegerField(null=False,
                                              verbose_name='Novice Debaters')

    date = models.DateField(blank=False)

    qual = models.BooleanField(default=True,
                               verbose_name='QUAL',
                               help_text='Does this tournament give qual points?')
    noty = models.BooleanField(default=True,
                               verbose_name='NOTY',
                               help_text='Does this tournament give NOTY points?')
    soty = models.BooleanField(default=True,
                               verbose_name='SOTY',
                               help_text='Does this tournament give SOTY points?')
    toty = models.BooleanField(default=True,
                               verbose_name='TOTY',
                               help_text='Does this tournament give TOTY points?')

    qual_bar = models.IntegerField(default=0,
                                   verbose_name='Qual Bar',
                                   help_text='If this tournament gives autoquals, this value represents the highest place that receivs the autoqual')


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

    def get_absolute_url(self):
        return reverse('core:tournament_detail', kwargs={'pk': self.id})


    def __str__(self):
        return self.name


    def save(self, *args, **kwargs):
        if self.name == '':
            previous_tournaments = Tournament.objects.filter(
                season=self.season
            ).exclude(
                id=self.id
            ).count()
            
            suffix = ' '
            
            if previous_tournaments:
                suffix += 'I' * (previous_tournaments + 1)
            
            self.name = self.host.name + suffix

        super().save(*args, **kwargs)
