from django.db import models

from django.conf import settings

from core.models.team import Team
from core.models.standings.base import AbstractStanding
from core.models.tournament import Tournament


class TOTY(AbstractStanding):

    team = models.ForeignKey(Team,
                             on_delete=models.CASCADE,
                             related_name='toty')

    points = models.FloatField(default=-1)

    marker_one = models.FloatField(default=0)
    marker_two = models.FloatField(default=0)
    marker_three = models.FloatField(default=0)
    marker_four = models.FloatField(default=0)
    marker_five = models.FloatField(default=0)
    marker_six = models.FloatField(default=0)

    tournament_one = models.ForeignKey(Tournament,
                                       on_delete=models.CASCADE,
                                       related_name='toty_tournament_one',
                                       null=True)
    tournament_two = models.ForeignKey(Tournament,
                                       on_delete=models.CASCADE,
                                       related_name='toty_tournament_two',
                                       null=True)
    tournament_three = models.ForeignKey(Tournament,
                                         on_delete=models.CASCADE,
                                         related_name='toty_tournament_three',
                                       null=True)
    tournament_four = models.ForeignKey(Tournament,
                                        on_delete=models.CASCADE,
                                        related_name='toty_tournament_four',
                                       null=True)
    tournament_five = models.ForeignKey(Tournament,
                                        on_delete=models.CASCADE,
                                        related_name='toty_tournament_five',
                                       null=True)
    tournament_six = models.ForeignKey(Tournament,
                                       on_delete=models.CASCADE,
                                       related_name='toty_tournament_six',
                                       null=True)

    class Meta:
        ordering = ('place',)

class TOTYReaff(models.Model):
    season = models.CharField(max_length=16)

    old_team = models.ForeignKey(Team,
                             on_delete=models.CASCADE,
                             related_name='toty_reaff_old')
    
    new_team = models.ForeignKey(Team,
                             on_delete=models.CASCADE,
                             related_name='toty_reaff_new')
    reaff_date = models.DateField()
    
    def __save__(self, *args, **kwargs):
        if not self.season:
            self.season = settings.CURRENT_SEASON
        super().save(*args, **kwargs)

    