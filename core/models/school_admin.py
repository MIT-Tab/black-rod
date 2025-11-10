from django.conf import settings
from django.db import models
from django.db.models import Q

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
    primary = models.BooleanField(default=False)

    class Meta:
        unique_together = ('user', 'school')
        ordering = ['school__name', 'user__username']
        constraints = [
            models.UniqueConstraint(
                fields=('school',),
                condition=Q(primary=True),
                name='unique_primary_school_admin'
            )
        ]

    def __str__(self):
        return f"{self.user.username} - {self.school.name}"
