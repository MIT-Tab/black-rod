from django.conf import settings as django_settings
from django.db import models


class SchedulerWorkspace(models.Model):
    name = models.CharField(max_length=100, unique=True, default="default")
    schools_csv = models.TextField(blank=True)
    dates_csv = models.TextField(blank=True)
    schools_filename = models.CharField(max_length=255, blank=True)
    dates_filename = models.CharField(max_length=255, blank=True)
    settings = models.JSONField(default=dict, blank=True)
    version = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="scheduler_workspaces_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Scheduler workspace"
        verbose_name_plural = "Scheduler workspaces"

    def __str__(self):
        return self.name

    @classmethod
    def get_solo(cls):
        workspace, _ = cls.objects.get_or_create(name="default")
        return workspace

    @property
    def has_inputs(self):
        return bool(self.schools_csv.strip() and self.dates_csv.strip())


class SchedulingRun(models.Model):
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = (
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    )

    workspace = models.ForeignKey(
        SchedulerWorkspace,
        on_delete=models.CASCADE,
        related_name="runs",
    )
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="scheduler_runs_created",
    )
    workspace_version = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_COMPLETED,
    )
    settings_snapshot = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    output_text = models.TextField(blank=True)
    error_text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Scheduling run {self.pk or 'unsaved'}"
