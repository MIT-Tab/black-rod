from django.conf import settings
from django.db import models

from .debater import Debater


class MergeDebaterRequest(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_DENIED = "denied"

    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_DENIED, "Denied"),
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="debater_merge_requests",
    )
    primary_debater = models.ForeignKey(
        Debater,
        on_delete=models.SET_NULL,
        related_name="primary_merge_requests",
        null=True,
        blank=True,
    )
    primary_name = models.CharField(max_length=128, blank=True)
    primary_school_name = models.CharField(max_length=128, blank=True)
    secondary_debater = models.ForeignKey(
        Debater,
        on_delete=models.SET_NULL,
        related_name="secondary_merge_requests",
        null=True,
        blank=True,
    )
    secondary_name = models.CharField(max_length=128, blank=True)
    secondary_school_name = models.CharField(max_length=128, blank=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processed_debater_merge_requests",
    )
    denial_reason = models.TextField(blank=True)

    class Meta:
        ordering = ("created_at",)
        constraints = [
            models.CheckConstraint(
                check=~models.Q(primary_debater=models.F("secondary_debater")),
                name="merge_request_distinct_debaters",
            ),
        ]

    def __str__(self):
        return f"Merge request {self.pk} ({self.primary_name or self.primary_debater_id} <- {self.secondary_name or self.secondary_debater_id})"

    @property
    def is_pending(self):
        return self.status == self.STATUS_PENDING

    def save(self, *args, **kwargs):
        if self.primary_debater:
            if not self.primary_name:
                self.primary_name = self.primary_debater.name
            if not self.primary_school_name and self.primary_debater.school:
                self.primary_school_name = self.primary_debater.school.name
        if self.secondary_debater:
            if not self.secondary_name:
                self.secondary_name = self.secondary_debater.name
            if not self.secondary_school_name and self.secondary_debater.school:
                self.secondary_school_name = self.secondary_debater.school.name
        super().save(*args, **kwargs)
