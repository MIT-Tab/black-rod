from django.conf import settings
from django.db import models
from django.shortcuts import reverse

from .debater_alias_group import DebaterAliasGroup
from .school import School


class Debater(models.Model):
    first_name = models.CharField(max_length=32, blank=False)

    last_name = models.CharField(max_length=32, blank=True, default='')

    alias_group = models.ForeignKey(
        DebaterAliasGroup,
        on_delete=models.SET_NULL,
        related_name="debaters",
        blank=True,
        null=True,
    )

    school = models.ForeignKey(
        School,
        on_delete=models.SET_NULL,
        related_name="debaters",
        blank=True,
        null=True,
    )
    # WHAT IF AFFILIATION CHANGES ?  Considered new debater

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="claimed_debaters",
        blank=True,
        null=True,
        help_text="User who has claimed this debater profile",
    )

    paradigm = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Link to Google Doc paradigm (must have sharing enabled)",
    )

    first_season = models.CharField(max_length=16, blank=True, null=True)
    latest_season = models.CharField(max_length=16, blank=True, null=True)

    NOVICE = 0
    VARSITY = 1
    DINO = 2
    STATUS = ((VARSITY, "Varsity"), (NOVICE, "Novice"), (DINO, "Dino"))
    status = models.IntegerField(choices=STATUS, default=VARSITY)
    dino_to_contact_opt_in = models.BooleanField(
        default=False,
        help_text="If enabled, tournaments know this DINO is open to TO/observer outreach.",
    )
    dino_judge_contact_opt_in = models.BooleanField(
        default=False,
        help_text="If enabled, tournaments know this DINO is open to judging outreach.",
    )

    def save(self, *args, **kwargs):
        if not self.pk:
            current_season = settings.CURRENT_SEASON
            if not self.first_season:
                self.first_season = current_season
            if not self.latest_season:
                self.latest_season = current_season
        # Only clear judge opt-in for non-dinos; TO opt-in is available for all statuses
        if self.status != self.DINO:
            self.dino_judge_contact_opt_in = False

        super().save(*args, **kwargs)

        for team in self.teams.all():
            team.update_name()
            team.save()

    @property
    def name(self):
        name = f"{self.first_name} {self.last_name}"
        return name.strip()

    @property
    def is_dino(self):
        return self.status == self.DINO

    def get_absolute_url(self):
        return reverse("core:debater_detail", kwargs={"pk": self.id})

    def __str__(self):
        return self.name


class QualPoints(models.Model):
    # THIS IS FUNCTIONALLY QUAL POINTS (EXCLUDED 6 FOR QUALLING ITSELF)

    debater = models.ForeignKey(
        Debater, on_delete=models.CASCADE, related_name="qual_points"
    )

    points = models.FloatField(default=0)

    season = models.CharField(max_length=16)

    @property
    def qualled(self):
        return self.debater.quals.filter(season=self.season).exists()

    def __save__(self, *args, **kwargs):
        if not self.season:
            self.season = settings.CURRENT_SEASON
        super().save(*args, **kwargs)


class Reaff(models.Model):
    season = models.CharField(max_length=16)

    old_debater = models.ForeignKey(
        Debater, on_delete=models.CASCADE, related_name="reaff_old"
    )

    new_debater = models.ForeignKey(
        Debater, on_delete=models.CASCADE, related_name="reaff_new"
    )
    reaff_date = models.DateField()

    def __save__(self, *args, **kwargs):
        if not self.season:
            self.season = settings.CURRENT_SEASON
        super().save(*args, **kwargs)
