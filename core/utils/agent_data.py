# pylint: disable=protected-access,unidiomatic-typecheck
"""Correction vocabulary and a row journal shared by API writes and undo.

PostgreSQL table locks deliberately cover ordinary admin/import writes too: a
merge must not observe half of a concurrent import. SQLite takes its writer lock
before reading. Only changed rows, not the surrounding snapshot, are persisted.
"""
import hashlib
import json
import uuid
from functools import partial

from django.apps import apps
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, connection, models, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import (
    APIException,
    PermissionDenied,
    ValidationError as APIValidationError,
)

from core.models import (
    AgentChangeSet,
    AgentCredential,
    Debater,
    DebaterAlias,
    Round,
    RoundStats,
    Team,
    SyntheticResolutionLog,
)
from core.utils.synthetic_resolution import resolve_synthetic_entity
from core.utils.merge import merge_debaters
from core.search_signals import synchronize_debaters
from core.models.round import sanitize_round_stat_values
from core.views.elo_cache import invalidate_cached_elo_state

# An explicit allowlist, never arbitrary model or field access from request data.
RESOURCES = {
    "rounds": (
        "Round",
        "gov_id opp_id tournament_id round_number stage division elim_size round_label metadata import_origin import_key victor".split(),
    ),
    "round_stats": (
        "RoundStats",
        "debater_id round_id stage speaks ranks debater_role score_index source_status metadata".split(),
    ),
    "round_metadata": (
        "ImportedRoundMetadata",
        "round_id gov_1_alias_id gov_2_alias_id opp_1_alias_id opp_2_alias_id raw_result_code raw_outcome_text".split(),
    ),
    "round_judges": (
        "ImportedRoundJudge",
        "round_metadata_id original_name debater_alias_id is_chair".split(),
    ),
    "imports": (
        "TournamentImport",
        "batch_id tournament_id import_type original_file_name source_hash imported_at".split(),
    ),
    "import_batches": ("ImportBatch", "created_at".split()),
    "aliases": (
        "DebaterAlias",
        "source_name normalized_name debater_id created_at updated_at".split(),
    ),
    "alias_groups": ("DebaterAliasGroup", "label".split()),
    "debaters": (
        "Debater",
        "first_name last_name school_id alias_group_id first_season latest_season status temporary synthetic elo_manual_opt".split(),
    ),
    "teams": ("Team", "name short_name synthetic".split()),
    "schools": (
        "School",
        "name short_name included_in_oty temporary synthetic".split(),
    ),
    "school_lookups": ("SchoolLookup", "server_name school_id".split()),
    "tournaments": (
        "Tournament",
        "name short_name manual_name num_rounds host_id season num_teams num_novice_teams num_debaters num_novice_debaters date qual noty soty toty online_qual_points autoqual_bar name_suffix qual_type".split(),
    ),
    "team_results": (
        "TeamResult",
        "tournament_id team_id type_of_place place ghost_points counts_for_points".split(),
    ),
    "speaker_results": (
        "SpeakerResult",
        "tournament_id debater_id type_of_place place tie counts_for_points".split(),
    ),
    "qualifications": (
        "QUAL",
        "season place points tied debater_id qual_type tournament_id".split(),
    ),
    "reaffiliations": (
        "Reaff",
        "season old_debater_id new_debater_id reaff_date".split(),
    ),
    "team_reaffiliations": (
        "TOTYReaff",
        "season old_team_id new_team_id reaff_date".split(),
    ),
}
READ_ONLY = {
    "synthetic_logs": "SyntheticResolutionLog",
    "qual_points": "QualPoints",
    "coty": "COTY",
    "noty": "NOTY",
    "online_qual": "OnlineQUAL",
    "soty": "SOTY",
    "toty": "TOTY",
}
M2M_FIELDS = {"teams": {"debaters"}, "round_metadata": {"sources"}}
MAX_OPERATIONS = 200
MAX_CHANGES = 20000


class Conflict(APIException):
    status_code = 409
    default_detail = "The data has changed. Read it again before editing."


def normalized(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True))


def digest(value):
    return hashlib.sha256(
        json.dumps(normalized(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def resource_model(resource):
    if resource in RESOURCES:
        return apps.get_model("core", RESOURCES[resource][0])
    if resource in READ_ONLY:
        return apps.get_model("core", READ_ONLY[resource])
    raise APIValidationError({"resource": "Unknown resource."})


def fields_for(resource, *, writable=False):
    model = resource_model(resource)
    permitted = RESOURCES.get(resource, (None, None))[1]
    return [
        f
        for f in model._meta.concrete_fields
        if (
            permitted is None
            or f.attname in permitted
            or (not writable and f.primary_key)
        )
        and (
            not writable
            or (
                not f.primary_key
                and f.editable
                and not getattr(f, "auto_now", False)
                and not getattr(f, "auto_now_add", False)
            )
        )
    ]


def m2m_for(resource):
    return [
        f
        for f in resource_model(resource)._meta.local_many_to_many
        if f.name in M2M_FIELDS.get(resource, ())
    ]


def row_data(obj):
    return normalized(
        {f.attname: getattr(obj, f.attname) for f in obj._meta.concrete_fields}
    )


def membership_ids(obj, field):
    through = field.remote_field.through
    return list(
        through._base_manager.filter(**{field.m2m_field_name() + "_id": obj.pk})
        .order_by(field.m2m_reverse_field_name() + "_id")
        .values_list(field.m2m_reverse_field_name() + "_id", flat=True)
    )


def revision(obj):
    data = row_data(obj)
    for f in obj._meta.local_many_to_many:
        data[f.name] = membership_ids(obj, f)
    return digest(data)


def public_row(resource, obj):
    data = normalized(
        {f.attname: getattr(obj, f.attname) for f in fields_for(resource)}
    )
    for f in m2m_for(resource):
        data[f.name] = membership_ids(obj, f)
    data["revision"] = revision(obj)
    return data


def all_core_models():
    return sorted(
        apps.get_app_config("core").get_models(include_auto_created=True),
        key=lambda m: m._meta.db_table,
    )


def lock_writes(credential):
    if connection.vendor == "postgresql":
        # Stable order prevents competing agent batches from deadlocking.
        tables = ", ".join(
            connection.ops.quote_name(m._meta.db_table) for m in all_core_models()
        )
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '10s'")
            cursor.execute(f"LOCK TABLE {tables} IN SHARE ROW EXCLUSIVE MODE")
    else:
        AgentCredential.objects.filter(pk=credential.pk).update(
            last_used_at=credential.last_used_at
        )
    # Recheck after obtaining the lock, including revocation during authentication.
    current = AgentCredential.objects.select_related("created_by").get(pk=credential.pk)
    if (
        current.revoked_at
        or current.expires_at <= timezone.now()
        or not current.can_write
        or not current.created_by.is_active
        or not current.created_by.is_superuser
    ):
        raise PermissionDenied("Credential is no longer authorized to write.")


class Journal:
    def __init__(self):
        self.scopes = {}
        self.before = {}

    def watch(self, model, query):
        # Django drops an empty Q() from "a | Q()", which would silently narrow a
        # whole-table watch to an earlier, smaller scope. Spell "all rows" out.
        if not query:
            query = Q(pk__isnull=False)
        label = model._meta.label
        previous_scope = self.scopes.get(label)
        if label in self.scopes:
            self.scopes[label] |= query
        else:
            self.scopes[label] = query
        # Add only unseen rows: a later operation must retain the batch preimage.
        # A row already inside the earlier scope but absent from `before` was
        # created by this batch, so its preimage is "did not exist".
        previously_watched = (
            set(
                model._base_manager.filter(previous_scope, query).values_list(
                    "pk", flat=True
                )
            )
            if previous_scope is not None
            else set()
        )
        for obj in model._base_manager.filter(query).order_by("pk"):
            key = (label, obj.pk)
            if key not in self.before:
                self.before[key] = (
                    None if obj.pk in previously_watched else row_data(obj)
                )

    def watch_row(self, obj):
        self.watch(type(obj), Q(pk=obj.pk))

    def created(self, obj):
        self.scopes.setdefault(obj._meta.label, Q(pk__in=[]))
        self.scopes[obj._meta.label] |= Q(pk=obj.pk)
        self.before[(obj._meta.label, obj.pk)] = None

    def watch_references(self, seeds):
        for model in all_core_models():
            query = Q(pk__in=[])
            for f in model._meta.concrete_fields:
                if f.is_relation and f.related_model in seeds:
                    query |= Q(**{f.attname + "__in": seeds[f.related_model]})
            if query != Q(pk__in=[]):
                self.watch(model, query)

    def watch_delete(self, obj):
        # Traverse all incoming FKs (including PROTECT/SET_NULL and through rows).
        # Restrict traversal to descendants so deleting a round never scans peers.
        pending = [obj]
        seen = set()
        while pending:
            parent = pending.pop()
            key = (parent._meta.label, parent.pk)
            if key in seen:
                continue
            seen.add(key)
            self.watch_row(parent)
            for rel in parent._meta.get_fields(include_hidden=True):
                if (
                    not rel.auto_created
                    or rel.concrete
                    or not (rel.one_to_many or rel.one_to_one)
                ):
                    continue
                model = rel.related_model
                children = model._base_manager.filter(**{rel.field.attname: parent.pk})
                self.watch(model, Q(**{rel.field.attname: parent.pk}))
                if rel.on_delete is models.CASCADE:
                    pending.extend(children)

    def changes(self):
        after = {}
        for label, query in self.scopes.items():
            model = apps.get_model(label)
            for obj in model._base_manager.filter(query).order_by("pk"):
                after[(label, obj.pk)] = row_data(obj)
        changes = [
            {
                "model": key[0],
                "id": key[1],
                "before": self.before.get(key),
                "after": after.get(key),
            }
            for key in sorted(self.before.keys() | after.keys())
            if self.before.get(key) != after.get(key)
        ]
        if len(changes) > MAX_CHANGES:
            raise APIValidationError(
                f"Batch changes more than {MAX_CHANGES} rows; use smaller batches."
            )
        return changes


def check_revision(obj, expected):
    if not isinstance(expected, str) or revision(obj) != expected:
        raise Conflict(
            {
                "model": obj._meta.label,
                "id": obj.pk,
                "detail": "Missing or stale revision.",
            }
        )


def resolve_refs(value, refs):
    if isinstance(value, str) and value.startswith("$"):
        if value[1:] not in refs:
            raise APIValidationError("Unknown operation reference: " + value)
        return refs[value[1:]]
    if isinstance(value, list):
        return [resolve_refs(v, refs) for v in value]
    return value


def watch_side_effects(journal, obj):
    journal.watch_row(obj)
    if isinstance(obj, Round):
        journal.watch(RoundStats, Q(round_id=obj.pk))
    if isinstance(obj, Debater):
        team_ids = list(Team.objects.filter(debaters=obj).values_list("pk", flat=True))
        journal.watch(Team, Q(pk__in=team_ids))


def merge(journal, operation, credential, reason):
    entity = (
        "debater"
        if operation["op"] == "merge_debaters"
        else operation.get("entity_type")
    )
    if entity not in {"debater", "school", "team"}:
        raise APIValidationError("entity_type must be debater, school, or team.")
    model = apps.get_model("core", entity)
    source = model._base_manager.get(pk=operation.get("source_id"))
    target = model._base_manager.get(pk=operation.get("target_id"))
    if source.pk == target.pk:
        raise APIValidationError("Cannot merge an identity into itself.")
    check_revision(source, operation.get("source_revision"))
    check_revision(target, operation.get("target_revision"))
    seeds = {model: [source.pk, target.pk]}
    if entity == "debater":
        seeds[Team] = list(
            Team.objects.filter(debaters__pk__in=seeds[model]).values_list(
                "pk", flat=True
            )
        )
        seeds[DebaterAlias] = list(
            DebaterAlias.objects.filter(debater_id__in=seeds[model]).values_list(
                "pk", flat=True
            )
        )
    for seed_model, ids in seeds.items():
        journal.watch(seed_model, Q(pk__in=ids))
    journal.watch_references(seeds)
    journal.watch_delete(source)
    # Recomputing rankings can reorder every competitor in an affected season.
    # These tables are small and include rows that a merge may create.
    for name in ("COTY", "NOTY", "OnlineQUAL", "QUAL", "QualPoints", "SOTY", "TOTY"):
        journal.watch(apps.get_model("core", name), Q())
    journal.watch(SyntheticResolutionLog, Q(entity_type=entity, synthetic_id=source.pk))
    source_id, target_id = source.pk, target.pk
    if operation["op"] == "merge_debaters":
        merge_debaters(primary=target, secondary=source)
    else:
        resolve_synthetic_entity(
            entity,
            source.pk,
            target.pk,
            actor=credential.created_by,
            reason=reason,
            source_context={"agent_credential_id": credential.pk},
        )
    return {"entity_type": entity, "source_id": source_id, "target_id": target_id}


def mutate(journal, operation, refs, credential, reason):
    if not isinstance(operation, dict):
        raise APIValidationError("Each operation must be an object.")
    action = operation.get("op")
    allowed_keys = (
        {
            "op",
            "entity_type",
            "source_id",
            "target_id",
            "source_revision",
            "target_revision",
        }
        if action in {"merge_synthetic", "merge_debaters"}
        else {"op", "resource", "id", "revision", "data", "ref"}
    )
    if set(operation) - allowed_keys:
        raise APIValidationError("Unknown operation fields.")
    if action in {"merge_synthetic", "merge_debaters"}:
        return merge(journal, operation, credential, reason)
    resource = operation.get("resource")
    if resource not in RESOURCES or action not in {"create", "update", "delete"}:
        raise APIValidationError("Unknown operation or writable resource.")
    model = resource_model(resource)
    if action == "create":
        obj = model()
    else:
        obj = model._base_manager.get(pk=resolve_refs(operation.get("id"), refs))
        check_revision(obj, operation.get("revision"))
        watch_side_effects(journal, obj)
    if action == "delete":
        # High-level identity deletion can remove claimed profiles and non-round
        # content. Use explicit synthetic merges instead.
        if resource not in {
            "rounds",
            "round_stats",
            "round_metadata",
            "round_judges",
            "imports",
            "import_batches",
            "team_results",
            "speaker_results",
            "school_lookups",
            "qualifications",
            "reaffiliations",
            "team_reaffiliations",
        }:
            raise APIValidationError(
                "Delete is not supported for this resource; repair it or merge the synthetic identity."
            )
        journal.watch_delete(obj)
        pk = obj.pk
        obj.delete()
        return {"resource": resource, "id": pk, "deleted": True}
    data = operation.get("data")
    if not isinstance(data, dict) or (action == "update" and not data):
        raise APIValidationError("data must be an object (nonempty for updates).")
    allowed = {f.attname: f for f in fields_for(resource, writable=True)}
    m2m = {f.name: f for f in m2m_for(resource)}
    if set(data) - (allowed.keys() | m2m.keys()):
        raise APIValidationError(
            {
                "data": "Unknown or protected fields: "
                + ", ".join(sorted(set(data) - (allowed.keys() | m2m.keys())))
            }
        )
    for key, value in data.items():
        if key in m2m:
            continue
        field = allowed[key]
        setattr(obj, key, field.to_python(resolve_refs(value, refs)))
    if isinstance(obj, RoundStats):
        for key, value in sanitize_round_stat_values(
            obj.round, speaks=obj.speaks, ranks=obj.ranks, debater_role=obj.debater_role
        ).items():
            setattr(obj, key, value)
    # blank import_key intentionally represents missing identity, not a duplicate.
    excluded = [
        f.name
        for f in model._meta.fields
        if not f.editable
        or (
            f.attname not in data
            and (action == "update" or (f.has_default() and f.get_default() == ""))
        )
    ]
    obj.full_clean(exclude=excluded)
    if model._meta.model_name == "tournament" and obj.host_id is None:
        # Imported tournaments may legitimately lack a host. The normal naming
        # hook assumes one, so retain their supplied/manual name in that case.
        obj.name = obj.manual_name or obj.name
        obj.save_base()
    else:
        obj.save()
    if action == "create":
        journal.created(obj)
    for key, field in m2m.items():
        if key not in data:
            continue
        ids = resolve_refs(data[key], refs)
        if not isinstance(ids, list) or any(
            type(pk) is not int or pk <= 0 for pk in ids
        ):
            raise APIValidationError({key: "Expected a list of positive integer IDs."})
        if field.related_model._base_manager.filter(pk__in=ids).count() != len(
            set(ids)
        ):
            raise APIValidationError({key: "Related record does not exist."})
        through = field.remote_field.through
        fk = field.m2m_field_name() + "_id"
        journal.watch(through, Q(**{fk: obj.pk}))
        # The related manager hides synthetic debaters. Write through rows
        # directly so replacement can also remove hidden/synthetic members.
        related_fk = field.m2m_reverse_field_name() + "_id"
        current_ids = list(
            through._base_manager.filter(**{fk: obj.pk})
            .order_by("pk")
            .values_list(related_fk, flat=True)
        )
        ids = list(dict.fromkeys(ids))
        if current_ids != ids:
            through._base_manager.filter(**{fk: obj.pk}).delete()
            through._base_manager.bulk_create(
                [through(**{fk: obj.pk, related_fk: related_id}) for related_id in ids]
            )
    if isinstance(obj, Team) and "debaters" in data:
        obj.update_name()
        obj.save()
    return public_row(resource, obj)


def change_summary(change):
    resource = next(
        (
            key
            for key, (name, _) in RESOURCES.items()
            if name.lower() == change["model"].split(".")[-1].lower()
        ),
        None,
    )
    if resource is None:
        resource = next(
            (
                key
                for key, name in READ_ONLY.items()
                if name.lower() == change["model"].split(".")[-1].lower()
            ),
            None,
        )
    visible = {f.attname for f in fields_for(resource)} if resource else set()
    for name in M2M_FIELDS:
        for field in m2m_for(name):
            through = field.remote_field.through
            if through._meta.label == change["model"]:
                visible = {f.attname for f in through._meta.concrete_fields}
    # Unknown related tables are reversible, but never expose account/contact data.
    return {
        "model": change["model"],
        "id": change["id"],
        "action": (
            "create"
            if change["before"] is None
            else "delete" if change["after"] is None else "update"
        ),
        "before": {k: v for k, v in (change["before"] or {}).items() if k in visible},
        "after": {k: v for k, v in (change["after"] or {}).items() if k in visible},
    }


def changeset_data(batch):
    return {
        "id": batch.pk,
        "request_id": str(batch.request_id),
        "credential_id": batch.credential_id,
        "reason": batch.reason,
        "created_at": batch.created_at,
        "undoes": batch.undoes_id,
        "reversed_by": AgentChangeSet.objects.filter(undoes=batch)
        .values_list("pk", flat=True)
        .first(),
        "results": batch.results,
        "change_hash": digest(batch.changes),
        "changes": [change_summary(c) for c in batch.changes],
    }


def restore(changes):
    for c in changes:
        model = apps.get_model(c["model"])
        obj = model._base_manager.filter(pk=c["id"]).first()
        if (row_data(obj) if obj else None) != c["after"]:
            raise Conflict(
                {
                    "model": c["model"],
                    "id": c["id"],
                    "detail": "Later edits prevent undo.",
                }
            )
    # Delete/reinsert *only journaled rows*. Raw SQL intentionally bypasses
    # destructive CASCADE handlers and save hooks. Django's deferred FK constraints
    # allow related rows to be restored together with original primary keys.
    with connection.cursor() as cursor:
        for c in changes:
            model = apps.get_model(c["model"])
            cursor.execute(
                f"DELETE FROM {connection.ops.quote_name(model._meta.db_table)} WHERE {connection.ops.quote_name(model._meta.pk.column)} = %s",
                [c["id"]],
            )
        for c in changes:
            if c["before"] is None:
                continue
            model = apps.get_model(c["model"])
            fields = model._meta.concrete_fields
            columns = ", ".join(connection.ops.quote_name(f.column) for f in fields)
            values = [
                f.get_db_prep_save(f.to_python(c["before"][f.attname]), connection)
                for f in fields
            ]
            placeholders = ", ".join(["%s"] * len(fields))
            cursor.execute(
                f"INSERT INTO {connection.ops.quote_name(model._meta.db_table)} ({columns}) VALUES ({placeholders})",
                values,
            )
    connection.check_constraints()


@transaction.atomic
def execute_batch(credential, payload, *, undo_id=None):
    if not isinstance(payload, dict):
        raise APIValidationError("Expected a JSON object.")
    allowed_keys = {"request_id", "reason", "dry_run", "expected_change_hash"}
    if undo_id is None:
        allowed_keys.add("operations")
    if set(payload) - allowed_keys:
        raise APIValidationError(
            "Unknown request fields: " + ", ".join(sorted(set(payload) - allowed_keys))
        )
    try:
        request_id = uuid.UUID(str(payload.get("request_id")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise APIValidationError(
            "request_id must be a UUID (the idempotency key)."
        ) from exc
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise APIValidationError("A reason of 1–4000 characters is required.")
    dry_run = payload.get("dry_run", False)
    if type(dry_run) is not bool:
        raise APIValidationError("dry_run must be a boolean.")
    lock_writes(credential)
    request_hash = digest(
        {
            "payload": {k: v for k, v in payload.items() if k != "dry_run"},
            "undo_id": undo_id,
        }
    )
    existing = AgentChangeSet.objects.filter(request_id=request_id).first()
    if existing:
        if (
            existing.credential_id != credential.pk
            or existing.request_hash != request_hash
        ):
            raise Conflict("request_id has already been used for another request.")
        return changeset_data(existing)
    if undo_id is not None:
        original = AgentChangeSet.objects.get(pk=undo_id)
        if AgentChangeSet.objects.filter(undoes=original).exists():
            raise Conflict("This change set has already been reversed.")
        try:
            restore(original.changes)
        except IntegrityError as exc:
            raise Conflict(
                "Later references or constraints prevent undo; reverse dependent changes first."
            ) from exc
        changes = [
            dict(c, before=c["after"], after=c["before"]) for c in original.changes
        ]
        operations, results = [{"op": "undo", "changeset_id": undo_id}], []
    else:
        original = None
        operations = payload.get("operations")
        if (
            not isinstance(operations, list)
            or not 1 <= len(operations) <= MAX_OPERATIONS
        ):
            raise APIValidationError(
                f"operations must contain 1–{MAX_OPERATIONS} operations."
            )
        journal, refs, results = Journal(), {}, []
        for index, operation in enumerate(operations):
            try:
                result = mutate(journal, operation, refs, credential, reason)
                if operation.get("ref"):
                    ref = operation["ref"]
                    if not isinstance(ref, str) or ref in refs or "id" not in result:
                        raise APIValidationError(
                            "ref must be a unique name for an operation that returns an id."
                        )
                    refs[ref] = result["id"]
                results.append(result)
            except (
                APIException,
                ValidationError,
                ValueError,
                TypeError,
                ObjectDoesNotExist,
            ) as exc:
                if isinstance(exc, Conflict):
                    raise
                raise APIValidationError(
                    {"operation_index": index, "detail": str(exc)}
                ) from exc
        changes = journal.changes()
    if payload.get("expected_change_hash") and payload[
        "expected_change_hash"
    ] != digest(changes):
        raise Conflict("The preview has changed; run a new dry run.")
    if dry_run:
        result = {
            "dry_run": True,
            "change_hash": digest(changes),
            "results": results,
            "changes": [change_summary(c) for c in changes],
        }
        transaction.set_rollback(True)
        return result
    batch = AgentChangeSet.objects.create(
        credential=credential,
        request_id=request_id,
        request_hash=request_hash,
        reason=reason,
        operations=operations,
        changes=changes,
        results=results,
        undoes=original,
    )
    # Only successful commits invalidate derived results. Avoid wildcard cache clears.
    transaction.on_commit(invalidate_cached_elo_state, robust=True)
    if undo_id is not None:
        debater_ids = [c["id"] for c in changes if c["model"] == "core.Debater"]
        transaction.on_commit(partial(synchronize_debaters, debater_ids), robust=True)
    return changeset_data(batch)
