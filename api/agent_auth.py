"""Bearer-only agent authentication, independent of browser sessions."""

import hashlib

from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission

from core.models import AgentCredential


class AgentAuthentication(BaseAuthentication):
    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if (
            len(parts) != 2
            or parts[0].lower() != b"bearer"
            or not parts[1].startswith(b"br_agent_")
        ):
            raise AuthenticationFailed("A valid agent bearer credential is required.")
        token_hash = hashlib.sha256(parts[1]).hexdigest()
        credential = (
            AgentCredential.objects.select_related("created_by")
            .filter(token_hash=token_hash)
            .first()
        )
        now = timezone.now()
        if (
            not credential
            or credential.revoked_at
            or credential.expires_at <= now
            or not credential.created_by.is_active
            or not credential.created_by.is_superuser
        ):
            raise AuthenticationFailed("Invalid, expired, or revoked agent credential.")
        AgentCredential.objects.filter(pk=credential.pk).update(last_used_at=now)
        return credential.created_by, credential

    def authenticate_header(self, request):
        return "Bearer"


class AgentPermission(BasePermission):
    def has_permission(self, request, view):
        return isinstance(request.auth, AgentCredential) and (
            request.method in {"GET", "HEAD", "OPTIONS"} or request.auth.can_write
        )


class AgentAuthenticationSchema(OpenApiAuthenticationExtension):
    target_class = "api.agent_auth.AgentAuthentication"
    name = "agentBearer"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "br_agent_..."}
