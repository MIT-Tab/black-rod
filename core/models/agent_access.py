"""Admin-issued credentials and durable, reversible agent change sets."""

import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone


class AgentCredential(models.Model):
    name = models.CharField(max_length=128)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    can_write = models.BooleanField(default=False)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def clean(self):
        if self.expires_at and self.expires_at <= timezone.now():
            raise ValidationError({"expires_at": "Expiration must be in the future."})

    @classmethod
    def issue(cls, *, admin, name, expires_at, can_write=False):
        if not admin.is_active or not admin.is_superuser:
            raise ValueError("Only an active superuser can issue agent credentials.")
        if expires_at <= timezone.now():
            raise ValueError("Expiration must be in the future.")
        token = "br_agent_" + secrets.token_urlsafe(32)
        credential = cls.objects.create(
            name=name,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            created_by=admin,
            expires_at=expires_at,
            can_write=can_write,
        )
        return credential, token

    def __str__(self):
        return self.name


class AgentChangeSet(models.Model):
    credential = models.ForeignKey(AgentCredential, on_delete=models.PROTECT)
    request_id = models.UUIDField(unique=True)
    request_hash = models.CharField(max_length=64)
    reason = models.TextField()
    operations = models.JSONField()
    # Full concrete database rows, including through rows and deleted records.
    changes = models.JSONField()
    results = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    undoes = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversal",
    )

    class Meta:
        ordering = ["-id"]
