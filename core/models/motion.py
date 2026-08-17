from django.conf import settings
from django.db import models
from django.shortcuts import reverse
from taggit.managers import TaggableManager

from core.models.tournament import Tournament


class Motion(models.Model):
    text = models.TextField(unique=True, verbose_name="Motion text")
    background_slide = models.TextField(blank=True)
    date_set = models.DateField(blank=True, null=True)
    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.SET_NULL,
        related_name="motions",
        blank=True,
        null=True,
    )
    tags = TaggableManager(through="core.TaggedMotion", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-date_set", "-pk")

    def get_absolute_url(self):
        return reverse("core:motion_detail", kwargs={"pk": self.pk})

    def __str__(self):
        return self.text


class MotionUserStatus(models.Model):
    DONE = "done"
    IGNORE = "ignore"
    STATUS_CHOICES = ((DONE, "Done"), (IGNORE, "Ignore"))

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="motion_statuses",
    )
    motion = models.ForeignKey(
        Motion, on_delete=models.CASCADE, related_name="user_statuses"
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("user", "motion"), name="unique_user_motion_status"
            )
        ]

    def __str__(self):
        return f"{self.user}: {self.motion_id} ({self.status})"
