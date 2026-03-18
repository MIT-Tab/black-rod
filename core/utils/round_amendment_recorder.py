import json
import os
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from tempfile import NamedTemporaryFile
from uuid import uuid4

from django.conf import settings

from core.models import ImportedRoundMetadata, SyntheticResolutionLog
from core.models.round import sanitize_round_stat_values


class RoundAmendmentRecordingError(ValueError):
    pass


def round_amendment_recording_enabled():
    return str(getattr(settings, "ENV", "") or "").strip().lower() == "development"


def round_amendment_recording_path():
    configured = str(getattr(settings, "ROUND_AMENDMENTS_FILE", "") or "").strip()
    if configured:
        return configured
    return os.path.join(str(getattr(settings, "BASE_DIR", "") or ""), "round-amendments.local.json")


def round_amendment_recording_context():
    return {
        "enabled": round_amendment_recording_enabled(),
        "path": round_amendment_recording_path(),
    }


def ensure_development_round_import_key(round_obj):
    if not round_amendment_recording_enabled():
        return None
    if getattr(round_obj, "pk", None):
        return None
    if str(getattr(round_obj, "import_key", "") or "").strip():
        return None
    generated = f"manual-amendment-{uuid4().hex[:24]}"
    round_obj.import_key = generated
    return generated


def record_round_amendment_action(action):
    if not round_amendment_recording_enabled():
        return None

    document, file_path, directory = _load_round_amendment_document()
    document.setdefault("actions", []).append(_json_safe(deepcopy(action)))
    _write_round_amendment_document(document, file_path=file_path, directory=directory)
    return file_path


def backfill_synthetic_resolution_actions_from_logs(source="synthetic_resolution_suggestions"):
    if not round_amendment_recording_enabled():
        return {
            "recorded": 0,
            "skipped": 0,
            "file_path": None,
        }

    document, file_path, directory = _load_round_amendment_document()
    actions = document.setdefault("actions", [])
    existing_signatures = {
        _synthetic_resolution_signature(action)
        for action in actions
        if _synthetic_resolution_signature(action) is not None
    }

    recorded = 0
    skipped = 0
    queryset = SyntheticResolutionLog.objects.filter(
        action=SyntheticResolutionLog.Action.RESOLVED,
        source_context__source=str(source or "").strip(),
    ).order_by("created_at", "id")

    for log in queryset:
        action = build_synthetic_resolution_action(
            entity_type=log.entity_type,
            synthetic_id=log.synthetic_id,
            target_id=log.resolved_to_id,
            reason=log.reason,
        )
        signature = _synthetic_resolution_signature(action)
        if signature in existing_signatures:
            skipped += 1
            continue
        actions.append(_json_safe(deepcopy(action)))
        existing_signatures.add(signature)
        recorded += 1

    if recorded:
        _write_round_amendment_document(document, file_path=file_path, directory=directory)

    return {
        "recorded": recorded,
        "skipped": skipped,
        "file_path": file_path,
    }


def build_synthetic_resolution_action(*, entity_type, synthetic_id, target_id, reason=""):
    return {
        "type": "resolve_synthetic",
        "entity_type": str(entity_type or "").strip().lower(),
        "synthetic_id": int(synthetic_id),
        "target_id": int(target_id),
        "reason": str(reason or ""),
    }


def _load_round_amendment_document():
    file_path = round_amendment_recording_path()
    directory = os.path.dirname(file_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    document = {"actions": []}
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                existing = json.load(handle)
        except json.JSONDecodeError as exc:
            raise RoundAmendmentRecordingError(
                f"Existing amendment file is not valid JSON: {file_path}"
            ) from exc
        if not isinstance(existing, dict):
            raise RoundAmendmentRecordingError(
                "Existing amendment file must contain a top-level JSON object."
            )
        actions = existing.get("actions")
        if actions is None:
            existing["actions"] = []
        elif not isinstance(actions, list):
            raise RoundAmendmentRecordingError(
                "Existing amendment file must contain an 'actions' list."
            )
        document = existing
    return document, file_path, directory


def _write_round_amendment_document(document, *, file_path, directory):
    document["last_updated_at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=directory or ".") as handle:
        json.dump(document, handle, indent=2, sort_keys=False)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, file_path)


def _synthetic_resolution_signature(action):
    if not isinstance(action, dict):
        return None
    if action.get("type") != "resolve_synthetic":
        return None
    try:
        return (
            "resolve_synthetic",
            str(action.get("entity_type") or "").strip().lower(),
            int(action.get("synthetic_id")),
            int(action.get("target_id")),
            str(action.get("reason") or ""),
        )
    except (TypeError, ValueError):
        return None


def build_round_delete_action(round_obj):
    action = {"type": "delete_round"}
    action.update(_round_identifier(round_obj))
    return action


def build_tournament_import_delete_action(import_row):
    return {
        "type": "delete_tournament_import",
        "id": int(import_row.id),
    }


def build_tournament_import_move_action(import_row, target_tournament_id):
    return {
        "type": "move_tournament_import",
        "id": int(import_row.id),
        "target_tournament_id": int(target_tournament_id),
    }


def build_round_upsert_action(round_obj, *, action_type):
    if action_type not in {"create_round", "update_round"}:
        raise RoundAmendmentRecordingError(f"Unsupported round action type: {action_type}")

    action = {
        "type": action_type,
        "tournament_id": int(round_obj.tournament_id),
        "gov_debater_ids": _team_debater_ids(round_obj.gov),
        "opp_debater_ids": _team_debater_ids(round_obj.opp),
        "round_number": int(round_obj.round_number or 0),
        "stage": str(round_obj.stage or ""),
        "division": str(round_obj.division or "") or None,
        "elim_size": int(round_obj.elim_size) if round_obj.elim_size else None,
        "round_label": str(round_obj.round_label or ""),
        "victor": int(round_obj.victor or 0),
        "metadata": deepcopy(round_obj.metadata or {}),
        "import_origin": str(round_obj.import_origin or ""),
        "import_key": str(round_obj.import_key or ""),
        "stats": [
            {
                "debater_id": int(stat.debater_id),
                "speaks": _json_safe(stat_values["speaks"]),
                "ranks": _json_safe(stat_values["ranks"]),
                "debater_role": str(stat_values["debater_role"] or ""),
                "score_index": int(stat.score_index or 1),
                "source_status": str(stat.source_status or ""),
                "metadata": deepcopy(stat.metadata or {}),
            }
            for stat in round_obj.stats.select_related("debater").order_by("score_index", "id")
            for stat_values in [
                sanitize_round_stat_values(
                    round_obj,
                    speaks=stat.speaks,
                    ranks=stat.ranks,
                    debater_role=stat.debater_role,
                )
            ]
        ],
    }

    imported_metadata = _serialize_imported_metadata(round_obj)
    if imported_metadata is not None:
        action["imported_metadata"] = imported_metadata

    if action_type == "update_round":
        action.update(_round_identifier(round_obj))

    return _strip_none_values(action)


def _serialize_imported_metadata(round_obj):
    try:
        imported_metadata = round_obj.imported_metadata
    except ImportedRoundMetadata.DoesNotExist:
        return None

    payload = {
        "raw_result_code": str(imported_metadata.raw_result_code or ""),
        "raw_outcome_text": str(imported_metadata.raw_outcome_text or ""),
        "source_import_ids": list(
            imported_metadata.sources.order_by("id").values_list("id", flat=True)
        ),
        "judges": [],
    }
    slot_map = {
        "gov_1": imported_metadata.gov_1_alias,
        "gov_2": imported_metadata.gov_2_alias,
        "opp_1": imported_metadata.opp_1_alias,
        "opp_2": imported_metadata.opp_2_alias,
    }
    for key, alias in slot_map.items():
        if alias is None:
            continue
        payload[key] = {
            "debater_id": int(alias.debater_id),
            "source_name": str(alias.source_name or ""),
        }

    for judge in imported_metadata.judges.select_related("debater_alias").order_by("id"):
        payload["judges"].append(
            {
                "original_name": str(judge.original_name or ""),
                "is_chair": bool(judge.is_chair),
                "debater_id": int(judge.debater_alias.debater_id)
                if judge.debater_alias_id
                else None,
                "source_name": str(judge.debater_alias.source_name or "")
                if judge.debater_alias_id
                else "",
            }
        )

    return _strip_none_values(payload)


def _round_identifier(round_obj):
    import_key = str(getattr(round_obj, "import_key", "") or "").strip()
    if import_key:
        return {
            "tournament_id": int(round_obj.tournament_id),
            "import_key": import_key,
        }
    return {"round_id": int(round_obj.id)}


def _team_debater_ids(team):
    return [
        int(debater_id)
        for debater_id in team.debaters.order_by("id").values_list("id", flat=True)
    ]


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _strip_none_values(value):
    if isinstance(value, dict):
        return {
            key: _strip_none_values(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_strip_none_values(item) for item in value]
    return value
