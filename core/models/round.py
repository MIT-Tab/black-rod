from django.db import models
from django.shortcuts import reverse

from .debater import Debater
from .team import Team
from .tournament import Tournament


class Round(models.Model):
    class Stage(models.TextChoices):
        PRELIM = "prelim", "Prelim"
        OUTROUND = "outround", "Outround"

    class Division(models.TextChoices):
        VARSITY = "varsity", "Varsity"
        NOVICE = "novice", "Novice"

    gov = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="govs")
    opp = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="opps")
    tournament = models.ForeignKey(
        Tournament, on_delete=models.CASCADE, related_name="rounds"
    )

    round_number = models.IntegerField(default=0)
    stage = models.CharField(
        max_length=16,
        choices=Stage.choices,
        default=Stage.PRELIM,
        db_index=True,
    )
    division = models.CharField(
        max_length=16,
        choices=Division.choices,
        null=True,
        blank=True,
        db_index=True,
    )
    elim_size = models.PositiveSmallIntegerField(null=True, blank=True, db_index=True)
    round_label = models.CharField(max_length=32, blank=True, default="")
    is_rated = models.BooleanField(default=False, db_index=True)
    weight = models.FloatField(default=1.0)
    metadata = models.JSONField(default=dict, blank=True)
    import_origin = models.CharField(max_length=32, blank=True, default="")
    import_key = models.CharField(max_length=128, blank=True, default="", db_index=True)

    UNKNOWN = 0
    GOV = 1
    OPP = 2
    GOV_VIA_FORFEIT = 3
    OPP_VIA_FORFEIT = 4
    ALL_DROP = 5
    ALL_WIN = 6
    BYE = 7
    VICTOR_CHOICES = (
        (UNKNOWN, "UNKNOWN"),
        (GOV, "GOV"),
        (OPP, "OPP"),
        (GOV_VIA_FORFEIT, "GOV via Forfeit"),
        (OPP_VIA_FORFEIT, "OPP via Forfeit"),
        (ALL_DROP, "ALL DROP"),
        (ALL_WIN, "ALL WIN"),
        (BYE, "BYE"),
    )

    victor = models.IntegerField(choices=VICTOR_CHOICES, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tournament", "import_key"],
                condition=~models.Q(import_key=""),
                name="unique_round_import_key_per_tournament",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(stage="outround")
                    | models.Q(elim_size__isnull=True)
                ),
                name="round_prelim_requires_null_elim_size",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(elim_size__isnull=True)
                    | models.Q(stage="outround")
                ),
                name="round_elim_size_requires_outround_stage",
            ),
            models.CheckConstraint(
                check=models.Q(elim_size__isnull=True) | models.Q(elim_size__gte=2),
                name="round_elim_size_at_least_two",
            ),
        ]

    def get_absolute_url(self):
        return reverse("core:round_detail", kwargs={"pk": self.id})


class RoundStats(models.Model):
    debater = models.ForeignKey(
        Debater, on_delete=models.CASCADE, related_name="round_stats"
    )

    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="stats")

    speaks = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    ranks = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    debater_role = models.CharField(max_length=4, null=True, blank=True)
    score_index = models.PositiveSmallIntegerField(default=1)
    source_status = models.CharField(max_length=32, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["round", "debater", "score_index"],
                name="unique_round_stats_score_index",
            ),
        ]
