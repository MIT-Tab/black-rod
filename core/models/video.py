from django.db import models
from django.shortcuts import reverse

from taggit.managers import TaggableManager

from core.models.debater import Debater
from core.models.team import Team

from core.models.tournament import Tournament


class Video(models.Model):
    pm = models.ForeignKey(Debater,
                           on_delete=models.CASCADE,
                           related_name='pm_videos',
                           verbose_name='PM')

    lo = models.ForeignKey(Debater,
                           on_delete=models.CASCADE,
                           related_name='lo_videos',
                           verbose_name='LO')

    mg = models.ForeignKey(Debater,
                           on_delete=models.CASCADE,
                           related_name='mg_videos',
                           verbose_name='MG')

    mo = models.ForeignKey(Debater,
                           on_delete=models.CASCADE,
                           related_name='mo_videos',
                           verbose_name='MO')

    tournament = models.ForeignKey(Tournament,
                                   on_delete=models.CASCADE,
                                   related_name='videos')

    UNKNOWN = 0
    ROUND_ONE = 1
    ROUND_TWO = 2
    ROUND_THREE = 3
    ROUND_FOUR = 4
    ROUND_FIVE = 5
    VO = 6
    VQ = 7
    VS = 8
    VF = 9
    NQ = 10
    NS = 11
    NF = 12
    ROUNDS = (
        (UNKNOWN, 'UNKNOWN'),
        (ROUND_ONE, '1'),
        (ROUND_TWO, '2'),
        (ROUND_THREE, '3'),
        (ROUND_FOUR, '4'),
        (ROUND_FIVE, '5'),
        (VO, 'Varsity Octafinals'),
        (VQ, 'Varsity Quarterfinals'),
        (VS, 'Varsity Semifinals'),
        (VF, 'Varsity Finals'),
        (NQ, 'Novice Quarterfinals'),
        (NS, 'Novice Semifinals'),
        (NF, 'Novice Finals')
    )

    round = models.IntegerField(choices=ROUNDS,
                                default=UNKNOWN)

    case = models.TextField(blank=True)
    description = models.TextField(blank=True)

    link = models.CharField(max_length=4096)
    password = models.CharField(max_length=1024,
                                blank=True,
                                help_text='If needed')

    ALL = 0
    ACCOUNTS_ONLY = 1
    DEBATERS_IN_ROUND = 2
    PERMISSIONS = (
        (ALL, 'All'),
        (ACCOUNTS_ONLY, 'Accounts Only'),
        (DEBATERS_IN_ROUND, 'Debaters in Round')
    )
    permissions = models.IntegerField(choices=PERMISSIONS,
                                      default=DEBATERS_IN_ROUND)

    tags = TaggableManager(blank=True)

    def get_absolute_url(self):
        return reverse('core:video_detail', kwargs={'pk': self.id})

    def __str__(self):
        return '%s / %s / %s' % (self.tournament.name,
                                 self.tournament.get_season_display(),
                                 self.get_round_display())
