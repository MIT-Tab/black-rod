import json

from django.conf import settings
from django.db import models
from django.shortcuts import reverse
from django.utils.html import strip_tags

from .debater_alias_group import DebaterAliasGroup
from .school import School


class ActiveDebaterManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(temporary=False, synthetic=False)


def sanitize_debater_name_part(value, fallback=""):
    cleaned = strip_tags(str(value or "")).strip()
    return cleaned or fallback


class Debater(models.Model):
    class EloManualOpt(models.TextChoices):
        UNSET = "", "Unset"
        OPT_IN = "opt_in", "Opt-in"
        OPT_OUT = "opt_out", "Opt-out"

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
    REGION_CHOICES = (
        ("north", "North"),
        ("south", "South"),
        ("central", "Central"),
        ("midwest", "Midwest"),
        ("west", "West"),
        ("online", "Online"),
        ("other", "Other"),
    )
    status = models.IntegerField(choices=STATUS, default=VARSITY)
    dino_to_contact_opt_in = models.BooleanField(
        default=False,
        help_text="If enabled, tournaments know this DINO is open to TO/observer outreach.",
    )
    dino_judge_contact_opt_in = models.BooleanField(
        default=False,
        help_text="If enabled, tournaments know this DINO is open to judging outreach.",
    )
    region = models.JSONField(
        default=list,
        blank=True,
        help_text="Optional location tags we only show alongside outreach opt-ins.",
    )
    elo_manual_opt = models.CharField(
        max_length=8,
        choices=EloManualOpt.choices,
        blank=True,
        default=EloManualOpt.UNSET,
        db_index=True,
    )
    temporary = models.BooleanField(default=False, db_index=True)
    synthetic = models.BooleanField(default=False, db_index=True)

    objects = ActiveDebaterManager()
    all_objects = models.Manager()

    def save(self, *args, **kwargs):
        original_first_name = self.first_name
        original_last_name = self.last_name
        self.first_name = sanitize_debater_name_part(self.first_name, fallback="Removed")
        self.last_name = sanitize_debater_name_part(self.last_name)

        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = set(update_fields)
            if self.first_name != original_first_name:
                update_fields.add("first_name")
            if self.last_name != original_last_name:
                update_fields.add("last_name")
            kwargs["update_fields"] = update_fields

        if not self.pk:
            current_season = settings.CURRENT_SEASON
            if not self.first_season:
                self.first_season = current_season
            if not self.latest_season:
                self.latest_season = current_season
        # Only clear judge opt-in for non-dinos; TO opt-in is available for all statuses
        if self.status != self.DINO:
            self.dino_judge_contact_opt_in = False

        if isinstance(self.region, str):
            self.region = [self.region] if self.region else []

        if not self.is_open_to_outreach:
            self.region = []

        super().save(*args, **kwargs)

        for team in self.teams.all():
            team.update_name()
            team.save()

    @property
    def name(self):
        name = f"{self.first_name} {self.last_name}"
        return name.strip()

    @property
    def display_name(self):
        base = self.name
        return f"{base} (New)" if self.temporary else base

    @property
    def is_dino(self):
        return self.status == self.DINO

    @property
    def is_open_to_outreach(self):
        return bool(self.dino_to_contact_opt_in or self.dino_judge_contact_opt_in)

    @property
    def show_region(self):
        return bool(self.region_list and self.is_open_to_outreach)

    @property
    def region_list(self):
        if isinstance(self.region, list):
            return self.region
        if isinstance(self.region, str):
            try:
                parsed = json.loads(self.region)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
            return [self.region] if self.region else []
        return list(self.region or [])

    def get_region_display(self):
        mapping = dict(self.REGION_CHOICES)
        labels = [mapping.get(code, code) for code in self.region_list]
        return ", ".join(labels)

    def get_absolute_url(self):
        return reverse("core:debater_detail", kwargs={"pk": self.id})

    def __str__(self):
        return self.display_name


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
