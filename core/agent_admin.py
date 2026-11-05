"""Only active superusers may issue/revoke credentials or inspect undo preimages."""

import hashlib
import secrets

from django.contrib import admin
from django.template.response import TemplateResponse
from django.utils import timezone

from core.models import AgentChangeSet, AgentCredential


class SuperuserAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AgentCredential)
class AgentCredentialAdmin(SuperuserAdmin):
    list_display = (
        "name",
        "created_by",
        "can_write",
        "expires_at",
        "revoked_at",
        "last_used_at",
    )
    list_filter = ("can_write", "revoked_at")
    actions = ("revoke_credentials",)
    fields = (
        "name",
        "can_write",
        "expires_at",
        "created_by",
        "created_at",
        "revoked_at",
        "last_used_at",
    )
    readonly_fields = ("created_by", "created_at", "revoked_at", "last_used_at")

    def get_readonly_fields(self, request, obj=None):
        return self.fields if obj else self.readonly_fields

    def save_model(self, request, obj, form, change):
        if change:
            return
        token = "br_agent_" + secrets.token_urlsafe(32)
        obj.token_hash = hashlib.sha256(token.encode()).hexdigest()
        obj.created_by = request.user
        obj.save()
        request.issued_agent_token = token

    def response_add(self, request, obj, post_url_continue=None):
        context = {
            **self.admin_site.each_context(request),
            "title": "Agent credential issued",
            "credential": obj,
            "token": request.issued_agent_token,
        }
        response = TemplateResponse(
            request, "admin/agent_credential_issued.html", context
        )
        response["Cache-Control"] = "no-store, private"
        response["Referrer-Policy"] = "no-referrer"
        return response

    @admin.action(description="Revoke selected agent credentials immediately")
    def revoke_credentials(self, request, queryset):
        queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())


@admin.register(AgentChangeSet)
class AgentChangeSetAdmin(SuperuserAdmin):
    list_display = ("id", "credential", "reason", "created_at", "undoes")
    readonly_fields = tuple(f.name for f in AgentChangeSet._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
