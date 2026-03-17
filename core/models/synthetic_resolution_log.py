"""Persists an audit trail for resolving synthetic debaters, schools, or teams into canonical records, including actor/context snapshots."""


from django.conf import settings
from django.db import models


class SyntheticResolutionLog(models.Model):
    class EntityType(models.TextChoices):
        DEBATER = "debater", "Debater"
        SCHOOL = "school", "School"
        TEAM = "team", "Team"

    entity_type = models.CharField(max_length=16, choices=EntityType.choices, db_index=True)
    synthetic_id = models.PositiveIntegerField(db_index=True)
    synthetic_name = models.CharField(max_length=255, blank=True)
    resolved_to_id = models.PositiveIntegerField(db_index=True)
    resolved_to_name = models.CharField(max_length=255, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="synthetic_resolution_logs",
    )
    reason = models.TextField(blank=True)
    source_context = models.JSONField(default=dict, blank=True)
    synthetic_snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return (
            f"{self.get_entity_type_display()} {self.synthetic_id} -> "
            f"{self.resolved_to_id}"
        )
