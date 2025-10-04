from django.conf import settings
from django.db import models

from .school import School


class SchoolAdmin(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="school_admins"
    )
    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="admins"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'school')
        ordering = ['school__name', 'user__username']

    def __str__(self):
        return f"{self.user.username} - {self.school.name}"
