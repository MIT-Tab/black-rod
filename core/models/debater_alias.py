from django.db import models

from .debater import Debater


class DebaterAlias(models.Model):
    source_name = models.CharField(max_length=128, db_index=True)
    normalized_name = models.CharField(max_length=128, db_index=True)
    debater = models.ForeignKey(
        Debater,
        on_delete=models.CASCADE,
        related_name="aliases",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["source_name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["debater", "source_name"],
                name="unique_debater_alias_source_name",
            ),
        ]
        indexes = [
            models.Index(
                fields=["normalized_name", "debater"],
                name="debater_alias_lookup_idx",
            ),
        ]

    def __str__(self):
        return f"{self.source_name} -> {self.debater.name}"
