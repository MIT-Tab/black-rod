from django.db import models

from .debater_alias import DebaterAlias
from .round import Round
from .tournament import Tournament


class ImportBatch(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Import batch {self.id}"


class TournamentImport(models.Model):
    class ImportType(models.TextChoices):
        FILE_BACKUP = "file_backup", "File Backup"
        FORUM_POST = "forum_post", "Forum Post"
        MITTAB_BUNDLE = "mittab_bundle", "Mit-Tab Bundle"
        DB_INFERENCE = "db_inference", "Inferred From DB"
        OTHER = "other", "Other"

    batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.SET_NULL,
        related_name="tournament_imports",
        null=True,
        blank=True,
    )
    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="tournament_imports",
    )
    import_type = models.CharField(
        max_length=16,
        choices=ImportType.choices,
        db_index=True,
    )
    original_file_name = models.CharField(max_length=255, blank=True)
    source_hash = models.CharField(max_length=64, blank=True, db_index=True)
    # Older bundles omitted the cards key. Explicit null means MIT-Tab withheld the cards.
    tab_cards = models.JSONField(default=dict, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["tournament_id", "-imported_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tournament", "import_type", "source_hash"],
                condition=~models.Q(source_hash=""),
                name="unique_round_tournament_import_hash",
            ),
        ]

    def __str__(self):
        return self.original_file_name or f"{self.tournament.name} {self.import_type}"


class ImportedRoundMetadata(models.Model):
    class SpeakerRole(models.TextChoices):
        PM = "PM", "PM"
        MG = "MG", "MG"
        LO = "LO", "LO"
        MO = "MO", "MO"

    round = models.OneToOneField(
        Round,
        on_delete=models.CASCADE,
        related_name="imported_metadata",
    )
    gov_1_alias = models.ForeignKey(
        DebaterAlias,
        on_delete=models.PROTECT,
        related_name="gov_1_round_metadata",
        null=True,
        blank=True,
    )
    gov_2_alias = models.ForeignKey(
        DebaterAlias,
        on_delete=models.PROTECT,
        related_name="gov_2_round_metadata",
        null=True,
        blank=True,
    )
    opp_1_alias = models.ForeignKey(
        DebaterAlias,
        on_delete=models.PROTECT,
        related_name="opp_1_round_metadata",
        null=True,
        blank=True,
    )
    opp_2_alias = models.ForeignKey(
        DebaterAlias,
        on_delete=models.PROTECT,
        related_name="opp_2_round_metadata",
        null=True,
        blank=True,
    )
    raw_result_code = models.CharField(max_length=32, blank=True)
    raw_outcome_text = models.TextField(blank=True)
    sources = models.ManyToManyField(
        TournamentImport,
        related_name="round_metadata",
        blank=True,
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(gov_1_alias__isnull=True)
                    | models.Q(gov_2_alias__isnull=True)
                    | ~models.Q(gov_1_alias=models.F("gov_2_alias"))
                ),
                name="imported_round_metadata_distinct_gov_aliases",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(opp_1_alias__isnull=True)
                    | models.Q(opp_2_alias__isnull=True)
                    | ~models.Q(opp_1_alias=models.F("opp_2_alias"))
                ),
                name="imported_round_metadata_distinct_opp_aliases",
            ),
        ]

    def __str__(self):
        return f"Imported metadata for round {self.round_id}"


class Judge(models.Model):
    name = models.CharField(max_length=128)
    source_tournament = models.ForeignKey(
        Tournament,
        on_delete=models.SET_NULL,
        related_name="imported_judges",
        null=True,
    )
    source_key = models.CharField(max_length=133)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_tournament", "source_key"],
                name="unique_judge_source_tournament_key",
            ),
        ]

    def __str__(self):
        return self.name


class ImportedRoundJudge(models.Model):
    round_metadata = models.ForeignKey(
        ImportedRoundMetadata,
        on_delete=models.CASCADE,
        related_name="judges",
    )
    original_name = models.CharField(max_length=128)
    judge = models.ForeignKey(
        Judge,
        on_delete=models.SET_NULL,
        related_name="round_assignments",
        null=True,
        blank=True,
    )
    debater_alias = models.ForeignKey(
        DebaterAlias,
        on_delete=models.SET_NULL,
        related_name="judged_rounds",
        null=True,
        blank=True,
    )
    is_chair = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["round_metadata"],
                condition=models.Q(is_chair=True),
                name="unique_round_metadata_chair_judge",
            ),
        ]

    def __str__(self):
        return self.original_name
