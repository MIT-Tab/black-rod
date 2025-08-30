from django.conf import settings
from django.db import models
from django.shortcuts import reverse

from .school import School


class Debater(models.Model):
    first_name = models.CharField(max_length=32, blank=False)

    last_name = models.CharField(max_length=32, blank=False)

    school = models.ForeignKey(
        School,
        on_delete=models.SET_NULL,
        related_name="debaters",
        blank=True,
        null=True,
    )
    # WHAT IF AFFILIATION CHANGES ?  Considered new debater
    
    first_season = models.CharField(max_length=16, blank=True, null=True)
    latest_season = models.CharField(max_length=16, blank=True, null=True)

    NOVICE = 0
    VARSITY = 1
    STATUS = ((VARSITY, "Varsity"), (NOVICE, "Novice"))
    status = models.IntegerField(choices=STATUS, default=VARSITY)

    def save(self, *args, **kwargs):
        if not self.pk:
            current_season = settings.CURRENT_SEASON
            if not self.first_season:
                self.first_season = current_season
            if not self.most_recent_season:
                self.most_recent_season = current_season
        super().save(*args, **kwargs)

        for team in self.teams.all():
            team.update_name()
            team.save()

    @property
    def name(self):
        name = f"{self.first_name} {self.last_name}"
        return name.strip()

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
