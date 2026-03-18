import json
from collections import Counter

from django.db import IntegrityError, transaction
from django.db.models import Q

from core.models import (
    Debater,
    DebaterAlias,
    ImportedRoundJudge,
    ImportedRoundMetadata,
    Round,
    RoundStats,
    School,
    Team,
    Tournament,
    TournamentImport,
)
from core.models.round import sanitize_round_stat_values
from core.utils.elo_runtime_engine.cache import clear_runtime_caches
from core.utils.synthetic_resolution import resolve_synthetic_entity
from core.utils.team import get_or_create_team_for_debaters
from core.views.elo_cache import invalidate_cached_elo_state


class RoundAmendmentError(ValueError):
    pass


ROUND_ACTION_TYPES = {
    "create_round",
    "update_round",
    "delete_round",
    "delete_tournament_import",
    "move_tournament_import",
    "resolve_synthetic",
}


ROUND_METADATA_SLOT_FIELDS = {
    "gov_1": "gov_1_alias",
    "gov_2": "gov_2_alias",
    "opp_1": "opp_1_alias",
    "opp_2": "opp_2_alias",
}


def load_round_amendment_document(uploaded_file):
    try:
        raw_bytes = uploaded_file.read()
        decoded = raw_bytes.decode("utf-8-sig")
        return json.loads(decoded)
    except UnicodeDecodeError as exc:
        raise RoundAmendmentError("Amendment files must be valid UTF-8 JSON.") from exc
    except json.JSONDecodeError as exc:
        raise RoundAmendmentError(f"Invalid JSON: {exc.msg}.") from exc


def apply_round_amendments(document, *, actor=None, source_context=None):
    actions = _normalize_actions(document)
    summary = Counter()
    source_context = dict(source_context or {})

    try:
        with transaction.atomic():
            for index, action in enumerate(actions, start=1):
                _apply_action(
                    action,
                    summary=summary,
                    actor=actor,
                    source_context=source_context,
                    action_index=index,
                )
    except (
        IntegrityError,
        Debater.DoesNotExist,
        DebaterAlias.DoesNotExist,
        Round.DoesNotExist,
        Team.DoesNotExist,
        Tournament.DoesNotExist,
        TournamentImport.DoesNotExist,
    ) as exc:
        raise RoundAmendmentError(str(exc)) from exc

    clear_runtime_caches()
    invalidate_cached_elo_state()

    normalized_summary = {
        "actions_applied": len(actions),
        "synthetic_resolutions": int(summary["synthetic_resolutions"]),
        "rounds_created": int(summary["rounds_created"]),
        "rounds_updated": int(summary["rounds_updated"]),
        "rounds_deleted": int(summary["rounds_deleted"]),
        "rounds_moved": int(summary["rounds_moved"]),
        "tournament_imports_deleted": int(summary["tournament_imports_deleted"]),
        "tournament_imports_moved": int(summary["tournament_imports_moved"]),
        "linked_source_imports_moved": int(summary["linked_source_imports_moved"]),
    }
    return normalized_summary


def _normalize_actions(document):
    if not isinstance(document, dict):
        raise RoundAmendmentError("Amendment JSON must be an object at the top level.")

    actions = []
    explicit_actions = document.get("actions") or []
    if explicit_actions:
        if not isinstance(explicit_actions, list):
            raise RoundAmendmentError("The 'actions' field must be a list.")
        for action in explicit_actions:
            normalized = _coerce_action(action, fallback_type=None)
            actions.append(normalized)

    for resolution in document.get("synthetic_resolutions") or []:
        actions.append(_coerce_action(resolution, fallback_type="resolve_synthetic"))

    round_groups = document.get("rounds") or {}
    if round_groups and not isinstance(round_groups, dict):
        raise RoundAmendmentError("The 'rounds' field must be an object.")
    for field_name, action_type in (
        ("create", "create_round"),
        ("update", "update_round"),
        ("delete", "delete_round"),
    ):
        for action in round_groups.get(field_name) or []:
            actions.append(_coerce_action(action, fallback_type=action_type))

    import_groups = document.get("tournament_imports") or {}
    if import_groups and not isinstance(import_groups, dict):
        raise RoundAmendmentError("The 'tournament_imports' field must be an object.")
    for field_name, action_type in (
        ("delete", "delete_tournament_import"),
        ("move", "move_tournament_import"),
    ):
        for action in import_groups.get(field_name) or []:
            actions.append(_coerce_action(action, fallback_type=action_type))

    if not actions:
        raise RoundAmendmentError("No amendment actions were found in the JSON file.")

    return actions


def _coerce_action(action, *, fallback_type):
    if not isinstance(action, dict):
        raise RoundAmendmentError("Each amendment action must be an object.")

    action_type = str(action.get("type") or action.get("action") or fallback_type or "").strip()
    if not action_type:
        raise RoundAmendmentError("Each amendment action must declare a type.")
    if action_type not in ROUND_ACTION_TYPES:
        raise RoundAmendmentError(f"Unsupported amendment action type: {action_type}.")

    normalized = dict(action)
    normalized["type"] = action_type
    return normalized


def _apply_action(action, *, summary, actor, source_context, action_index):
    action_type = action["type"]
    contextual_source = dict(source_context)
    contextual_source["action_index"] = action_index
    contextual_source["action_type"] = action_type

    if action_type == "resolve_synthetic":
        resolve_synthetic_entity(
            entity_type=_required_str(action, "entity_type"),
            synthetic_id=_required_int(action, "synthetic_id"),
            target_id=_required_int(action, "target_id"),
            actor=actor,
            reason=str(action.get("reason") or ""),
            source_context=contextual_source,
        )
        summary["synthetic_resolutions"] += 1
        return

    if action_type == "create_round":
        _create_round(action, summary=summary)
        return

    if action_type == "update_round":
        _update_round(action, summary=summary)
        return

    if action_type == "delete_round":
        _delete_round(action, summary=summary)
        return

    if action_type == "delete_tournament_import":
        _delete_tournament_import(action, summary=summary)
        return

    if action_type == "move_tournament_import":
        _move_tournament_import(action, summary=summary)
        return

    raise RoundAmendmentError(f"Unhandled amendment action type: {action_type}.")


def _create_round(action, *, summary):
    tournament = Tournament.objects.get(pk=_required_int(action, "tournament_id"))
    round_obj = Round(tournament=tournament)

    gov_team = _resolve_team(action, "gov")
    opp_team = _resolve_team(action, "opp")
    if gov_team is None or opp_team is None:
        raise RoundAmendmentError("Round creation requires gov and opp team identifiers.")

    round_obj.gov = gov_team
    round_obj.opp = opp_team
    _apply_round_fields(round_obj, action, partial=False)
    round_obj.save()

    metadata_row = None
    if "imported_metadata" in action:
        metadata_row = _apply_imported_metadata(round_obj, action.get("imported_metadata"))
        _sync_round_teams_from_metadata(round_obj, action.get("imported_metadata"))
    _replace_round_stats(round_obj, action.get("stats"), metadata_row=metadata_row)

    summary["rounds_created"] += 1


def _update_round(action, *, summary):
    round_obj = _resolve_round(action)

    new_tournament_id = action.get("tournament_id")
    if new_tournament_id is not None:
        round_obj.tournament = Tournament.objects.get(pk=int(new_tournament_id))

    gov_team = _resolve_team(action, "gov")
    if gov_team is not None:
        round_obj.gov = gov_team

    opp_team = _resolve_team(action, "opp")
    if opp_team is not None:
        round_obj.opp = opp_team

    _apply_round_fields(round_obj, action, partial=True)
    round_obj.save()

    metadata_row = None
    if "imported_metadata" in action:
        metadata_row = _apply_imported_metadata(round_obj, action.get("imported_metadata"))
        _sync_round_teams_from_metadata(round_obj, action.get("imported_metadata"))
    if "stats" in action:
        _replace_round_stats(round_obj, action.get("stats"), metadata_row=metadata_row)

    summary["rounds_updated"] += 1


def _delete_round(action, *, summary):
    round_obj = _resolve_round(action)
    round_obj.delete()
    summary["rounds_deleted"] += 1


def _delete_tournament_import(action, *, summary):
    import_row = _resolve_tournament_import(action)
    round_ids = list(
        Round.objects.filter(imported_metadata__sources=import_row)
        .distinct()
        .values_list("id", flat=True)
    )
    if round_ids:
        Round.objects.filter(id__in=round_ids).delete()
        summary["rounds_deleted"] += len(round_ids)
    import_row.delete()
    summary["tournament_imports_deleted"] += 1


def _move_tournament_import(action, *, summary):
    import_row = _resolve_tournament_import(action)
    target_tournament = Tournament.objects.get(pk=_required_int(action, "target_tournament_id"))
    source_tournament_id = import_row.tournament_id

    affected_round_ids = list(
        Round.objects.filter(imported_metadata__sources=import_row)
        .distinct()
        .values_list("id", flat=True)
    )
    _validate_round_move_conflicts(affected_round_ids, target_tournament.id)
    _validate_import_move_conflicts(import_row, target_tournament.id)

    linked_source_import_ids = list(
        TournamentImport.objects.filter(
            round_metadata__round_id__in=affected_round_ids,
            tournament_id=source_tournament_id,
        )
        .distinct()
        .values_list("id", flat=True)
    )
    if not linked_source_import_ids:
        linked_source_import_ids = [import_row.id]
    _validate_import_move_conflicts_for_ids(linked_source_import_ids, target_tournament.id)

    if affected_round_ids:
        Round.objects.filter(id__in=affected_round_ids).update(tournament=target_tournament)
        summary["rounds_moved"] += len(affected_round_ids)

    moved_imports = TournamentImport.objects.filter(id__in=linked_source_import_ids).update(
        tournament=target_tournament
    )
    summary["tournament_imports_moved"] += 1
    summary["linked_source_imports_moved"] += moved_imports


def _resolve_round(action):
    round_id = action.get("round_id", action.get("id"))
    if round_id is not None:
        return Round.objects.get(pk=int(round_id))

    import_key = str(action.get("import_key") or "").strip()
    if not import_key:
        raise RoundAmendmentError("Round actions require either 'id'/'round_id' or 'import_key'.")

    queryset = Round.objects.filter(import_key=import_key)
    tournament_id = action.get("tournament_id")
    if tournament_id is not None:
        queryset = queryset.filter(tournament_id=int(tournament_id))

    matches = list(queryset[:2])
    if not matches:
        raise RoundAmendmentError(f"No round found for import_key '{import_key}'.")
    if len(matches) > 1:
        raise RoundAmendmentError(
            f"Multiple rounds matched import_key '{import_key}'; include tournament_id."
        )
    return matches[0]


def _resolve_tournament_import(action):
    import_id = action.get("tournament_import_id", action.get("id"))
    if import_id is None:
        raise RoundAmendmentError("Tournament import actions require 'id' or 'tournament_import_id'.")
    return TournamentImport.objects.get(pk=int(import_id))


def _resolve_team(action, prefix):
    nested = action.get(prefix)
    if nested is not None and not isinstance(nested, dict):
        raise RoundAmendmentError(f"'{prefix}' must be an object when provided.")

    team_id = action.get(f"{prefix}_team_id")
    debater_ids = action.get(f"{prefix}_debater_ids")
    if team_id is None and nested:
        team_id = nested.get("team_id")
    if debater_ids is None and nested:
        debater_ids = nested.get("debater_ids")

    if team_id is not None:
        return Team.objects.get(pk=int(team_id))

    if debater_ids is None:
        return None
    if not isinstance(debater_ids, list) or len(debater_ids) != 2:
        raise RoundAmendmentError(f"'{prefix}_debater_ids' must contain exactly two debater ids.")

    debaters = [
        Debater.all_objects.get(pk=int(debater_id))
        for debater_id in debater_ids
    ]
    return get_or_create_team_for_debaters(debaters[0], debaters[1])


def _apply_round_fields(round_obj, action, *, partial):
    if "round_number" in action or not partial:
        round_obj.round_number = int(action.get("round_number") or 0)
    if "stage" in action:
        round_obj.stage = _validate_choice("stage", action.get("stage"), Round.Stage.values)
    elif not partial and not round_obj.stage:
        round_obj.stage = Round.Stage.PRELIM
    if "division" in action:
        division = action.get("division")
        round_obj.division = None if division in (None, "") else _validate_choice(
            "division",
            division,
            Round.Division.values,
        )
    if "elim_size" in action:
        elim_size = action.get("elim_size")
        round_obj.elim_size = None if elim_size in (None, "") else int(elim_size)
    if "round_label" in action:
        round_obj.round_label = str(action.get("round_label") or "")
    if "victor" in action:
        round_obj.victor = int(action.get("victor"))
    if "metadata" in action:
        round_obj.metadata = _coerce_dict(action.get("metadata"), field_name="metadata")
    if "import_origin" in action:
        round_obj.import_origin = str(action.get("import_origin") or "")
    if "import_key" in action:
        round_obj.import_key = str(action.get("import_key") or "")


def _replace_round_stats(round_obj, stats_payload, *, metadata_row=None):
    if stats_payload is None:
        RoundStats.objects.filter(round=round_obj).delete()
        return
    if not isinstance(stats_payload, list):
        raise RoundAmendmentError("Round stats must be provided as a list.")

    RoundStats.objects.filter(round=round_obj).delete()
    stat_rows = []
    seen_keys = set()
    for entry in stats_payload:
        if not isinstance(entry, dict):
            raise RoundAmendmentError("Each round stat entry must be an object.")
        debater_id = entry.get("debater_id")
        if debater_id in (None, ""):
            slot_ref = str(entry.get("slot_ref") or "").strip()
            if not slot_ref:
                raise RoundAmendmentError("Each round stat entry requires either debater_id or slot_ref.")
            if slot_ref not in ROUND_METADATA_SLOT_FIELDS:
                raise RoundAmendmentError(f"Unsupported round stat slot_ref: {slot_ref}.")
            metadata_source = metadata_row or getattr(round_obj, "imported_metadata", None)
            if metadata_source is None:
                raise RoundAmendmentError("Round stat slot_ref requires imported metadata on the round.")
            alias_field = ROUND_METADATA_SLOT_FIELDS[slot_ref]
            alias = getattr(metadata_source, alias_field, None)
            if alias is None:
                raise RoundAmendmentError(
                    f"Round stat slot_ref '{slot_ref}' could not be resolved to an imported alias."
                )
            debater_id = alias.debater_id
        debater_id = int(debater_id)
        score_index = int(entry.get("score_index") or 1)
        unique_key = (debater_id, score_index)
        if unique_key in seen_keys:
            raise RoundAmendmentError(
                f"Duplicate round stat payload for debater_id={debater_id}, score_index={score_index}."
            )
        seen_keys.add(unique_key)
        stat_values = sanitize_round_stat_values(
            round_obj,
            speaks=entry.get("speaks"),
            ranks=entry.get("ranks"),
            debater_role=(str(entry.get("debater_role") or "").strip() or None),
        )
        stat_rows.append(
            RoundStats(
                round=round_obj,
                debater=Debater.all_objects.get(pk=debater_id),
                stage=stat_values["stage"],
                speaks=stat_values["speaks"],
                ranks=stat_values["ranks"],
                debater_role=stat_values["debater_role"],
                score_index=score_index,
                source_status=str(entry.get("source_status") or ""),
                metadata=_coerce_dict(entry.get("metadata"), field_name="stat metadata"),
            )
        )
    if stat_rows:
        RoundStats.objects.bulk_create(stat_rows)


def _apply_imported_metadata(round_obj, payload):
    if payload is None:
        ImportedRoundMetadata.objects.filter(round=round_obj).delete()
        return None
    if not isinstance(payload, dict):
        raise RoundAmendmentError("Imported round metadata must be an object or null.")

    metadata_row, _ = ImportedRoundMetadata.objects.get_or_create(round=round_obj)
    legacy_slot_roles = {}

    for payload_key, alias_field in ROUND_METADATA_SLOT_FIELDS.items():
        if payload_key not in payload:
            continue
        slot_payload = payload.get(payload_key)
        if slot_payload is None:
            setattr(metadata_row, alias_field, None)
            continue
        if not isinstance(slot_payload, dict):
            raise RoundAmendmentError(f"'{payload_key}' metadata must be an object or null.")
        setattr(metadata_row, alias_field, _resolve_alias(slot_payload))
        role = str(slot_payload.get("role") or "").strip() or None
        if role:
            legacy_slot_roles[payload_key] = role

    if "raw_result_code" in payload:
        metadata_row.raw_result_code = str(payload.get("raw_result_code") or "")
    if "raw_outcome_text" in payload:
        metadata_row.raw_outcome_text = str(payload.get("raw_outcome_text") or "")

    metadata_row.full_clean()
    metadata_row.save()

    if "source_import_ids" in payload:
        source_import_ids = payload.get("source_import_ids") or []
        if not isinstance(source_import_ids, list):
            raise RoundAmendmentError("'source_import_ids' must be a list.")
        imports = list(TournamentImport.objects.filter(id__in=source_import_ids).order_by("id"))
        if len(imports) != len(set(int(item) for item in source_import_ids)):
            raise RoundAmendmentError("One or more source_import_ids did not match a tournament import.")
        mismatched_ids = [row.id for row in imports if row.tournament_id != round_obj.tournament_id]
        if mismatched_ids:
            raise RoundAmendmentError(
                "Imported metadata sources must belong to the same tournament as the round. "
                f"Mismatched import ids: {mismatched_ids}."
            )
        metadata_row.sources.set(imports)

    if "judges" in payload:
        judges_payload = payload.get("judges") or []
        if not isinstance(judges_payload, list):
            raise RoundAmendmentError("'judges' must be a list.")
        chair_count = 0
        for item in judges_payload:
            if not isinstance(item, dict):
                raise RoundAmendmentError("Each judge entry must be an object.")
            if _coerce_bool(item.get("is_chair", False)):
                chair_count += 1
        if chair_count > 1:
            raise RoundAmendmentError("Imported metadata cannot have more than one chair judge.")

        ImportedRoundJudge.objects.filter(round_metadata=metadata_row).delete()
        judge_rows = []
        for judge_payload in judges_payload:
            original_name = str(
                judge_payload.get("original_name")
                or judge_payload.get("source_name")
                or ""
            ).strip()
            if not original_name:
                raise RoundAmendmentError("Each judge entry requires an original_name.")
            debater_alias = None
            if judge_payload.get("alias_id") is not None or judge_payload.get("debater_id") is not None:
                alias_source_name = str(
                    judge_payload.get("source_name") or judge_payload.get("original_name") or ""
                ).strip()
                alias_payload = dict(judge_payload)
                alias_payload["source_name"] = alias_source_name
                debater_alias = _resolve_alias(alias_payload)
            judge_rows.append(
                ImportedRoundJudge(
                    round_metadata=metadata_row,
                    original_name=original_name,
                    debater_alias=debater_alias,
                    is_chair=_coerce_bool(judge_payload.get("is_chair", False)),
                )
            )
        if judge_rows:
            ImportedRoundJudge.objects.bulk_create(judge_rows)
    if legacy_slot_roles:
        _backfill_legacy_metadata_roles(round_obj, metadata_row, legacy_slot_roles)
    return metadata_row


def _backfill_legacy_metadata_roles(round_obj, metadata_row, slot_roles):
    if round_obj.stage == Round.Stage.OUTROUND:
        return

    for slot, role in slot_roles.items():
        alias_field = ROUND_METADATA_SLOT_FIELDS[slot]
        alias = getattr(metadata_row, alias_field, None)
        if alias is None:
            continue
        matching_stats = RoundStats.objects.filter(
            round=round_obj,
            debater_id=alias.debater_id,
        )
        if matching_stats.exclude(
            Q(debater_role__isnull=True) | Q(debater_role="")
        ).exists():
            continue
        matching_stats.filter(
            Q(debater_role__isnull=True) | Q(debater_role="")
        ).update(debater_role=role)


def _resolve_alias(payload):
    alias_id = payload.get("alias_id")
    if alias_id is not None:
        return DebaterAlias.objects.get(pk=int(alias_id))

    debater_id = payload.get("debater_id")
    create_synthetic = _coerce_bool(payload.get("create_synthetic", False))
    source_name = str(payload.get("source_name") or "").strip()
    if debater_id is None and not create_synthetic:
        raise RoundAmendmentError(
            "Alias payloads require either alias_id, debater_id, or create_synthetic=true."
        )

    if debater_id is not None:
        debater = Debater.all_objects.get(pk=int(debater_id))
        if not source_name:
            source_name = str(debater.name or "").strip()
    else:
        if not source_name:
            raise RoundAmendmentError(
                "Synthetic alias payloads require a source_name when debater_id is omitted."
            )
        first_name = str(payload.get("first_name") or "").strip()
        last_name = str(payload.get("last_name") or "").strip()
        if not first_name:
            parts = source_name.split()
            first_name = parts[0]
            last_name = " ".join(parts[1:]) if len(parts) > 1 else last_name
        existing_aliases = list(
            DebaterAlias.objects.filter(
                source_name=source_name,
                debater__synthetic=True,
                debater__first_name=first_name,
                debater__last_name=last_name,
            ).select_related("debater")[:2]
        )
        if len(existing_aliases) > 1:
            raise RoundAmendmentError(
                f"Multiple synthetic aliases already exist for source_name '{source_name}'. "
                "Specify debater_id explicitly."
            )
        if existing_aliases:
            debater = existing_aliases[0].debater
        else:
            school = None
            school_id = payload.get("school_id")
            if school_id not in (None, ""):
                school = School.all_objects.get(pk=int(school_id))
            debater = Debater.all_objects.create(
                first_name=first_name,
                last_name=last_name,
                school=school,
                synthetic=True,
                temporary=False,
            )
    if not source_name:
        raise RoundAmendmentError("Alias payloads require a source_name or a debater with a name.")

    alias, created = DebaterAlias.objects.get_or_create(
        debater=debater,
        source_name=source_name,
        defaults={"normalized_name": source_name.casefold()},
    )
    normalized_name = source_name.casefold()
    if not created and alias.normalized_name != normalized_name:
        alias.normalized_name = normalized_name
        alias.save(update_fields=["normalized_name", "updated_at"])
    return alias


def _sync_round_teams_from_metadata(round_obj, metadata_payload):
    if not isinstance(metadata_payload, dict):
        return
    metadata_row = getattr(round_obj, "imported_metadata", None)
    if metadata_row is None:
        return

    updated = False
    for prefix, slots in (("gov", ("gov_1", "gov_2")), ("opp", ("opp_1", "opp_2"))):
        if not any(slot in metadata_payload for slot in slots):
            continue
        aliases = []
        for slot in slots:
            alias_field = ROUND_METADATA_SLOT_FIELDS[slot]
            alias = getattr(metadata_row, alias_field, None)
            if alias is None:
                aliases = []
                break
            aliases.append(alias)
        if len(aliases) != 2:
            continue
        team = get_or_create_team_for_debaters(aliases[0].debater, aliases[1].debater)
        if prefix == "gov":
            round_obj.gov = team
        else:
            round_obj.opp = team
        updated = True

    if updated:
        round_obj.save(update_fields=["gov", "opp"])


def _validate_round_move_conflicts(round_ids, target_tournament_id):
    if not round_ids:
        return
    import_keys = [
        key
        for key in Round.objects.filter(id__in=round_ids)
        .exclude(import_key="")
        .values_list("import_key", flat=True)
    ]
    if not import_keys:
        return
    conflict = (
        Round.objects.filter(tournament_id=target_tournament_id, import_key__in=import_keys)
        .exclude(id__in=round_ids)
        .order_by("id")
        .first()
    )
    if conflict:
        raise RoundAmendmentError(
            "Cannot move imported rounds because the target tournament already has a "
            f"round with import_key '{conflict.import_key}'."
        )


def _validate_import_move_conflicts(import_row, target_tournament_id):
    source_hash = str(import_row.source_hash or "").strip()
    if not source_hash:
        return
    conflict = (
        TournamentImport.objects.filter(
            tournament_id=target_tournament_id,
            import_type=import_row.import_type,
            source_hash=source_hash,
        )
        .exclude(id=import_row.id)
        .order_by("id")
        .first()
    )
    if conflict:
        raise RoundAmendmentError(
            "Cannot move tournament import because the target tournament already has "
            f"an import with source_hash '{source_hash}'."
        )


def _validate_import_move_conflicts_for_ids(import_ids, target_tournament_id):
    for import_row in TournamentImport.objects.filter(id__in=import_ids):
        _validate_import_move_conflicts(import_row, target_tournament_id)


def _required_int(payload, field_name):
    value = payload.get(field_name)
    if value in (None, ""):
        raise RoundAmendmentError(f"Missing required field: {field_name}.")
    return int(value)


def _required_str(payload, field_name):
    value = str(payload.get(field_name) or "").strip()
    if not value:
        raise RoundAmendmentError(f"Missing required field: {field_name}.")
    return value


def _coerce_dict(value, *, field_name):
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise RoundAmendmentError(f"{field_name.capitalize()} must be an object.")
    return value


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


def _validate_choice(field_name, value, allowed_values):
    normalized = str(value or "").strip()
    if normalized not in set(allowed_values):
        raise RoundAmendmentError(
            f"Invalid value for {field_name}: {normalized}."
        )
    return normalized
