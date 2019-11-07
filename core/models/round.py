from django.db import models

from .team import Team
from .debater import Debater
from .tournament import Tournament


class Round(models.Model):
    gov = models.ForeignKey(Team,
                            on_delete=models.CASCADE,
                            related_name='govs')
    
    opp = models.ForeignKey(Team,
                            on_delete=models.CASCADE,
                            related_name='opps')
    
    tournament = models.ForeignKey(Tournament,
                                   on_delete=models.CASCADE,
                                   related_name='rounds')
    
    round_number = models.IntegerField(default=0)
    
    UNKNOWN = 0
    GOV = 1
    OPP = 2
    GOV_VIA_FORFEIT = 3
    OPP_VIA_FORFEIT = 4
    ALL_DROP = 5
    ALL_WIN = 6
    VICTOR_CHOICES = (
        (UNKNOWN, "UNKNOWN"),
        (GOV, "GOV"),
        (OPP, "OPP"),
        (GOV_VIA_FORFEIT, "GOV via Forfeit"),
        (OPP_VIA_FORFEIT, "OPP via Forfeit"),
        (ALL_DROP, "ALL DROP"),
        (ALL_WIN, "ALL WIN"),
    )

    victor = models.IntegerField(choices=VICTOR_CHOICES,
                                 default=0)

    # JUDGES TO DO


class RoundStats(models.Model):
    debater = models.ForeignKey(Debater,
                                on_delete=models.CASCADE,
                                related_name='round_stats')

    round = models.ForeignKey(Round,
                              on_delete=models.CASCADE,
                              related_name='stats')

    speaks = models.DecimalField(max_digits=6,
                                 decimal_places=4)
    ranks = models.DecimalField(max_digits=6,
                                decimal_places=4)
    debater_role = models.CharField(max_length=4,
                                    null=True)
