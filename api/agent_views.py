# pylint: disable=protected-access
"""Authenticated discovery, correction data, atomic bulk changes, and undo."""
from pathlib import Path

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import DatabaseError, IntegrityError
from django.db.models import Q
from drf_spectacular.utils import extend_schema, OpenApiTypes
from rest_framework.exceptions import NotFound, ValidationError as APIValidationError
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import AgentChangeSet
from core.utils.agent_data import (
    Conflict,
    RESOURCES,
    READ_ONLY,
    MAX_CHANGES,
    MAX_OPERATIONS,
    changeset_data,
    execute_batch,
    fields_for,
    m2m_for,
    public_row,
    resource_model,
)
from .agent_auth import AgentAuthentication, AgentPermission


class AgentView(APIView):
    authentication_classes = [AgentAuthentication]
    permission_classes = [AgentPermission]
    parser_classes = [JSONParser]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store, private"
        return response

    def handle_exception(self, exc):
        if isinstance(exc, ObjectDoesNotExist):
            exc = NotFound("Record not found.")
        elif isinstance(exc, IntegrityError):
            exc = APIValidationError(
                "A database constraint prevents this change. The entire request was rolled back."
            )
        elif isinstance(exc, (ValidationError, ValueError, TypeError)):
            exc = APIValidationError(str(exc))
        elif isinstance(exc, DatabaseError):
            exc = Conflict(
                "The database could not safely complete the change. The request was rolled back; retry with the same request_id."
            )
        return super().handle_exception(exc)


class AgentDiscoveryView(AgentView):
    @extend_schema(
        responses=OpenApiTypes.OBJECT,
        description="Discover the correction API, allowed fields, relations, constraints, and supported bulk operations.",
    )
    def get(self, request):
        resources = {}
        for name in RESOURCES.keys() | READ_ONLY.keys():
            writable = (
                {f.attname for f in fields_for(name, writable=True)}
                if name in RESOURCES
                else set()
            )
            resources[name] = {
                "writable": name in RESOURCES,
                "fields": {
                    f.attname: {
                        "type": f.get_internal_type(),
                        "nullable": f.null,
                        "blank": f.blank,
                        "writable": f.attname in writable,
                        "required_on_create": not (
                            f.has_default()
                            or f.null
                            or f.blank
                            or f.primary_key
                            or not f.editable
                        ),
                        "choices": list(f.choices) if f.choices else None,
                        "related_model": (
                            f.related_model._meta.label if f.is_relation else None
                        ),
                    }
                    for f in fields_for(name)
                },
                "many_to_many": [f.name for f in m2m_for(name)],
            }
        return Response(
            {
                "version": 1,
                "transport": "REST",
                "can_write": request.auth.can_write,
                "resources": resources,
                "operations": [
                    "create",
                    "update",
                    "delete",
                    "merge_synthetic",
                    "merge_debaters",
                ],
                "max_operations": MAX_OPERATIONS,
                "max_changed_rows": MAX_CHANGES,
                "bulk_url": "/api/agent/v1/batches/",
                "docs": "/api/agent/v1/guide/",
                "undo_url_template": "/api/agent/v1/batches/{id}/undo/",
                "concurrency": "update/delete require revision; merge requires source_revision and target_revision",
            }
        )


class AgentGuideView(AgentView):
    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        guide = Path(__file__).resolve().parents[1] / "docs" / "agent-corrections.md"
        return Response({"guide": guide.read_text(encoding="utf-8")})


class AgentRecordsView(AgentView):
    @extend_schema(
        responses=OpenApiTypes.OBJECT,
        description="Read uncached correction records, including synthetic/temporary identities. Filter by exposed scalar fields (exact or __in), q text search, limit and after cursor.",
    )
    def get(self, request, resource, pk=None):
        model = resource_model(resource)
        qs = model._base_manager.order_by("pk")
        if pk is not None:
            return Response(public_row(resource, qs.get(pk=pk)))
        allowed = {f.attname: f for f in fields_for(resource)}
        for key, value in request.query_params.items():
            if key in {"q", "after", "limit"}:
                continue
            base = key.removesuffix("__in")
            if base not in allowed or allowed[base].get_internal_type() in {
                "JSONField",
                "TextField",
            }:
                raise APIValidationError({key: "Unknown or unsupported filter."})
            field = allowed[base]

            def parse_filter(raw, field=field):
                if field.get_internal_type() == "BooleanField" and raw.lower() in {
                    "true",
                    "false",
                }:
                    return raw.lower() == "true"
                return field.to_python(raw)

            if key.endswith("__in"):
                values = value.split(",")
                if len(values) > 200:
                    raise APIValidationError("At most 200 filter values.")
                parsed = [parse_filter(v) for v in values]
            else:
                parsed = None if value == "null" else parse_filter(value)
            qs = qs.filter(**{key: parsed})
        query = request.query_params.get("q", "")
        if query:
            search = Q(pk__in=[])
            for name, field in allowed.items():
                if field.get_internal_type() in {"CharField", "TextField"}:
                    search |= Q(**{name + "__icontains": query[:200]})
            qs = qs.filter(search)
        limit = int(request.query_params.get("limit", "100"))
        after = int(request.query_params.get("after", "0"))
        if not 1 <= limit <= 500 or after < 0:
            raise APIValidationError(
                "limit must be 1–500 and after must be nonnegative."
            )
        rows = list(qs.filter(pk__gt=after)[: limit + 1])
        return Response(
            {
                "results": [public_row(resource, obj) for obj in rows[:limit]],
                "next_after": rows[limit - 1].pk if len(rows) > limit else None,
            }
        )


class AgentBatchesView(AgentView):
    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, pk=None):
        if pk is not None:
            return Response(changeset_data(AgentChangeSet.objects.get(pk=pk)))
        limit = int(request.query_params.get("limit", "50"))
        before = int(request.query_params.get("before", str(2**63 - 1)))
        if not 1 <= limit <= 200 or before < 1:
            raise APIValidationError("Invalid history pagination.")
        rows = list(
            AgentChangeSet.objects.filter(pk__lt=before).order_by("-pk")[: limit + 1]
        )
        return Response(
            {
                "results": [
                    {
                        "id": b.pk,
                        "reason": b.reason,
                        "credential_id": b.credential_id,
                        "created_at": b.created_at,
                        "undoes": b.undoes_id,
                        "changed_rows": len(b.changes),
                    }
                    for b in rows[:limit]
                ],
                "next_before": rows[limit - 1].pk if len(rows) > limit else None,
            }
        )

    @extend_schema(
        request=OpenApiTypes.OBJECT,
        responses=OpenApiTypes.OBJECT,
        description="Atomic bulk correction. Requires request_id UUID, reason, and 1–200 operations. Optional dry_run and expected_change_hash. See authenticated guide for examples.",
    )
    def post(self, request):
        return Response(execute_batch(request.auth, request.data))


class AgentUndoView(AgentView):
    @extend_schema(
        request=OpenApiTypes.OBJECT,
        responses=OpenApiTypes.OBJECT,
        description="Reverse a committed batch. Requires a fresh request_id and reason. Supports dry_run. Refuses conflicting later edits. Undo itself is a reversible change set.",
    )
    def post(self, request, pk):
        return Response(execute_batch(request.auth, request.data, undo_id=pk))
