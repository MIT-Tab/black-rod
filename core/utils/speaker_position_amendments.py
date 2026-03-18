import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from django.db.models import Prefetch, Q

from core.models import Debater, ImportedRoundMetadata, Round, RoundStats, TournamentImport


SPEAKER_SLOTS = {
    "gov_1": "PM",
    "gov_2": "MG",
    "opp_1": "LO",
    "opp_2": "MO",
}
ROLE_TO_SLOT = {role: slot for slot, role in SPEAKER_SLOTS.items()}
RAW_BACKUP_IMPORT_TYPE = TournamentImport.ImportType.FILE_BACKUP
RAW_BACKUP_STAGE = Round.Stage.PRELIM
RAW_BACKUP_CANDIDATE_PATTERNS = (
    "final-backup.json",
    "manual_backup*.dump.sql",
    "before_pairing*.dump.sql",
    "*.dump.sql",
)


@dataclass(frozen=True)
class _RawBackupRound:
    round_number: int
    victor: int | None
    gov_names: tuple[str, str]
    opp_names: tuple[str, str]
    roles_by_name: dict[str, str]


@dataclass(frozen=True)
class _Participant:
    debater_id: int
    display_name: str
    normalized_names: frozenset[str]


def generate_speaker_position_amendment_document(
    *,
    source_root,
    debater_id=None,
    debater_name=None,
    round_ids=None,
):
    round_metadata_rows = _target_round_metadata_rows(
        debater_id=debater_id,
        debater_name=debater_name,
        round_ids=round_ids,
    )
    report_rows = []
    actions = []
    backup_cache = {}

    for metadata_row in round_metadata_rows:
        round_obj = metadata_row.round
        backup_sources = _candidate_backup_sources(metadata_row, source_root)
        if not backup_sources:
            status = "missing_file_backup_import"
            if metadata_row.sources.exists():
                status = "missing_backup_file"
            report_rows.append(_report_row(round_obj, metadata_row, status=status))
            continue

        successful_matches = []
        saw_round_match = False
        saw_name_mismatch = False
        for import_row, backup_path in backup_sources:
            raw_rounds = backup_cache.get(str(backup_path))
            if raw_rounds is None:
                raw_rounds = _load_raw_backup_rounds(backup_path)
                backup_cache[str(backup_path)] = raw_rounds

            matched_raw_round = _match_raw_round(round_obj, metadata_row, raw_rounds)
            if matched_raw_round is None:
                continue
            saw_round_match = True

            desired_roles = _map_backup_roles_to_current_debaters(metadata_row, matched_raw_round)
            if desired_roles is None:
                saw_name_mismatch = True
                continue
            successful_matches.append((import_row, backup_path, desired_roles))

        if not successful_matches:
            status = "unmatched_backup_names" if saw_name_mismatch else "no_unique_backup_match"
            import_row, backup_path = backup_sources[0]
            report_rows.append(
                _report_row(
                    round_obj,
                    metadata_row,
                    status=status,
                    import_row=import_row,
                    source_path=str(backup_path),
                )
            )
            continue

        desired_role_variants = {
            tuple(sorted(desired_roles.items()))
            for _, _, desired_roles in successful_matches
        }
        if len(desired_role_variants) != 1:
            import_row, backup_path, _ = successful_matches[0]
            report_rows.append(
                _report_row(
                    round_obj,
                    metadata_row,
                    status="no_unique_backup_match",
                    import_row=import_row,
                    source_path=str(backup_path),
                )
            )
            continue

        import_row, backup_path, desired_roles = successful_matches[0]

        action = _build_update_action(round_obj, metadata_row, desired_roles)
        if action is None:
            report_rows.append(
                _report_row(
                    round_obj,
                    metadata_row,
                    status="already_correct",
                    import_row=import_row,
                    source_path=str(backup_path),
                )
            )
            continue

        actions.append(action)
        report_rows.append(
            _report_row(
                round_obj,
                metadata_row,
                status="patched",
                import_row=import_row,
                source_path=str(backup_path),
            )
        )

    generated_at = _timestamp()
    description_target = ""
    if debater_id is not None:
        description_target = f" debater_id={int(debater_id)}"
    elif debater_name:
        description_target = f" debater_name={str(debater_name).strip()}"

    document = {
        "description": (
            "Correct inround speaker positions from raw backup files only."
            f"{description_target}"
        ),
        "generated_at": generated_at,
        "actions": actions,
    }
    report = {
        "generated_at": generated_at,
        "source_root": str(Path(source_root)),
        "summary": {
            "target_rounds": len(report_rows),
            "actions_written": len(actions),
            "already_correct": sum(1 for row in report_rows if row["status"] == "already_correct"),
            "missing_backup_file": sum(1 for row in report_rows if row["status"] == "missing_backup_file"),
            "missing_file_backup_import": sum(
                1 for row in report_rows if row["status"] == "missing_file_backup_import"
            ),
            "no_unique_backup_match": sum(
                1 for row in report_rows if row["status"] == "no_unique_backup_match"
            ),
            "unmatched_backup_names": sum(
                1 for row in report_rows if row["status"] == "unmatched_backup_names"
            ),
        },
        "rounds": report_rows,
    }
    return document, report


def write_amendment_document(*, document, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return output_path


def write_report_document(*, report, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return output_path


def _target_round_metadata_rows(*, debater_id, debater_name, round_ids):
    alias_prefetch = Prefetch("aliases")
    queryset = (
        ImportedRoundMetadata.objects.filter(
            round__stage=RAW_BACKUP_STAGE,
        )
        .select_related(
            "round",
            "round__tournament",
            "gov_1_alias",
            "gov_1_alias__debater",
            "gov_2_alias",
            "gov_2_alias__debater",
            "opp_1_alias",
            "opp_1_alias__debater",
            "opp_2_alias",
            "opp_2_alias__debater",
            "round__gov",
            "round__opp",
        )
        .prefetch_related(
            "sources",
            "round__stats",
            "round__gov__debaters__aliases",
            "round__opp__debaters__aliases",
        )
        .distinct()
        .order_by("round__tournament__date", "round__round_number", "round_id")
    )
    if round_ids:
        queryset = queryset.filter(round_id__in=[int(round_id) for round_id in round_ids])

    if debater_id is not None:
        debater_id = int(debater_id)
        queryset = queryset.filter(
            Q(gov_1_alias__debater_id=debater_id)
            | Q(gov_2_alias__debater_id=debater_id)
            | Q(opp_1_alias__debater_id=debater_id)
            | Q(opp_2_alias__debater_id=debater_id)
        )
        return list(queryset)

    if debater_name:
        normalized = _normalize_name(debater_name)
        matching_debater_ids = list(
            Debater.all_objects.filter(
                Q(first_name__isnull=False)
            ).prefetch_related(alias_prefetch)
        )
        target_ids = []
        for debater in matching_debater_ids:
            candidate_names = {_normalize_name(debater.name)}
            candidate_names.update(
                _normalize_name(alias.source_name) for alias in debater.aliases.all()
            )
            if normalized in candidate_names:
                target_ids.append(debater.id)
        if not target_ids:
            return []
        queryset = queryset.filter(
            Q(gov_1_alias__debater_id__in=target_ids)
            | Q(gov_2_alias__debater_id__in=target_ids)
            | Q(opp_1_alias__debater_id__in=target_ids)
            | Q(opp_2_alias__debater_id__in=target_ids)
        )
    return list(queryset)


def _candidate_backup_sources(metadata_row, source_root):
    candidates = []
    seen_paths = set()
    for import_row in metadata_row.sources.all():
        for backup_path in _derive_backup_paths(import_row, source_root):
            if str(backup_path) in seen_paths or not backup_path.exists():
                continue
            seen_paths.add(str(backup_path))
            candidates.append((import_row, backup_path))
    return candidates


def _derive_backup_paths(import_row, source_root):
    source_root = Path(source_root)
    original_file_name = str(import_row.original_file_name or "").strip()
    if not original_file_name:
        return []

    exact_path = _derive_backup_path_from_file_name(original_file_name, source_root)
    if exact_path is not None:
        return [exact_path]

    endpoint = _extract_endpoint(original_file_name)
    if endpoint is None:
        return []

    endpoint_dirs = sorted(source_root.glob(f"*/{endpoint}"))
    ranked_paths = []
    seen_paths = set()
    for endpoint_dir in endpoint_dirs:
        for pattern in RAW_BACKUP_CANDIDATE_PATTERNS:
            for path in sorted(endpoint_dir.glob(pattern), reverse=True):
                if path.is_file() and str(path) not in seen_paths:
                    seen_paths.add(str(path))
                    ranked_paths.append(path)
    return ranked_paths


def _derive_backup_path_from_file_name(original_file_name, source_root):
    original_file_name = str(original_file_name or "").strip()
    if not original_file_name.endswith(".json"):
        return None
    stem = Path(original_file_name).stem
    if "-" not in stem:
        return None
    parent_stem, endpoint = stem.rsplit("-", 1)
    if not endpoint.isdigit():
        return None
    return Path(source_root) / parent_stem / endpoint / "final-backup.json"


def _load_raw_backup_rounds(path):
    path = Path(path)
    if path.suffix == ".json":
        return _load_raw_backup_rounds_from_json(path)
    if path.name.endswith(".dump.sql"):
        return _load_raw_backup_rounds_from_sql_dump(path)
    return []


def _load_raw_backup_rounds_from_json(path):
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    by_model = defaultdict(list)
    for row in rows:
        by_model[str(row.get("model") or "")].append(row)

    debaters = {
        int(row["pk"]): str(row.get("fields", {}).get("name") or "").strip()
        for row in by_model["tab.debater"]
    }
    teams = {
        int(row["pk"]): tuple(int(pk) for pk in row.get("fields", {}).get("debaters") or [])
        for row in by_model["tab.team"]
    }
    roundstats_by_round = defaultdict(list)
    for row in by_model["tab.roundstats"]:
        fields = row.get("fields") or {}
        raw_role = _canonical_role(fields.get("debater_role"))
        debater_id = fields.get("debater")
        round_id = fields.get("round")
        if raw_role is None or debater_id is None or round_id is None:
            continue
        roundstats_by_round[int(round_id)].append((int(debater_id), raw_role))

    raw_rounds = []
    for row in by_model["tab.round"]:
        fields = row.get("fields") or {}
        gov_team_id = fields.get("gov_team")
        opp_team_id = fields.get("opp_team")
        round_number = fields.get("round_number")
        if gov_team_id is None or opp_team_id is None or round_number is None:
            continue
        gov_debaters = teams.get(int(gov_team_id)) or ()
        opp_debaters = teams.get(int(opp_team_id)) or ()
        if len(gov_debaters) != 2 or len(opp_debaters) != 2:
            continue
        roles_by_name = {}
        for debater_id, role in roundstats_by_round.get(int(row["pk"]), []):
            name = debaters.get(int(debater_id), "")
            if not name:
                continue
            roles_by_name[_normalize_name(name)] = role
        raw_rounds.append(
            _RawBackupRound(
                round_number=int(round_number),
                victor=_coerce_int(fields.get("victor")),
                gov_names=tuple(debaters.get(debater_id, "").strip() for debater_id in gov_debaters),
                opp_names=tuple(debaters.get(debater_id, "").strip() for debater_id in opp_debaters),
                roles_by_name=roles_by_name,
            )
        )
    return raw_rounds


def _load_raw_backup_rounds_from_sql_dump(path):
    table_columns = {}
    debaters = {}
    team_debaters = defaultdict(list)
    roundstats_by_round = defaultdict(list)
    round_rows = []

    with Path(path).open(encoding="utf-8") as handle:
        current_table = None
        current_columns = []
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            if current_table is not None:
                if line.startswith(") ENGINE="):
                    table_columns[current_table] = list(current_columns)
                    current_table = None
                    current_columns = []
                    continue
                match = re.match(r"\s*`([^`]+)`", line)
                if match:
                    current_columns.append(match.group(1))
                continue

            create_match = re.match(r"CREATE TABLE `([^`]+)` \($", line)
            if create_match and create_match.group(1) in {
                "tab_debater",
                "tab_team_debaters",
                "tab_round",
                "tab_roundstats",
            }:
                current_table = create_match.group(1)
                current_columns = []
                continue

            insert_match = re.match(
                r"INSERT INTO `([^`]+)`(?: \(([^)]*)\))? VALUES (.*);$",
                line,
            )
            if not insert_match:
                continue

            table_name = insert_match.group(1)
            if table_name not in {"tab_debater", "tab_team_debaters", "tab_round", "tab_roundstats"}:
                continue

            columns_spec = insert_match.group(2)
            if columns_spec:
                columns = [column.strip().strip("`") for column in columns_spec.split(",")]
            else:
                columns = table_columns.get(table_name, [])
            if not columns:
                continue

            for raw_values in _parse_sql_insert_values(insert_match.group(3)):
                row = dict(zip(columns, raw_values))
                if table_name == "tab_debater":
                    debater_id = _coerce_int(row.get("id"))
                    name = str(row.get("name") or "").strip()
                    if debater_id is not None and name:
                        debaters[debater_id] = name
                elif table_name == "tab_team_debaters":
                    team_id = _coerce_int(row.get("team_id"))
                    debater_id = _coerce_int(row.get("debater_id"))
                    if team_id is not None and debater_id is not None:
                        team_debaters[team_id].append(debater_id)
                elif table_name == "tab_roundstats":
                    debater_id = _coerce_int(row.get("debater_id") or row.get("debater"))
                    round_id = _coerce_int(row.get("round_id") or row.get("round"))
                    role = _canonical_role(row.get("debater_role"))
                    if debater_id is not None and round_id is not None and role is not None:
                        roundstats_by_round[round_id].append((debater_id, role))
                elif table_name == "tab_round":
                    round_rows.append(row)

    raw_rounds = []
    for row in round_rows:
        gov_team_id = _coerce_int(row.get("gov_team_id") or row.get("gov_team"))
        opp_team_id = _coerce_int(row.get("opp_team_id") or row.get("opp_team"))
        round_number = _coerce_int(row.get("round_number"))
        round_id = _coerce_int(row.get("id"))
        if gov_team_id is None or opp_team_id is None or round_number is None or round_id is None:
            continue

        gov_debaters = tuple(team_debaters.get(gov_team_id) or [])
        opp_debaters = tuple(team_debaters.get(opp_team_id) or [])
        if len(gov_debaters) != 2 or len(opp_debaters) != 2:
            continue

        roles_by_name = {}
        for debater_id, role in roundstats_by_round.get(round_id, []):
            name = debaters.get(debater_id, "")
            if not name:
                continue
            roles_by_name[_normalize_name(name)] = role

        raw_rounds.append(
            _RawBackupRound(
                round_number=round_number,
                victor=_coerce_int(row.get("victor")),
                gov_names=tuple(debaters.get(debater_id, "").strip() for debater_id in gov_debaters),
                opp_names=tuple(debaters.get(debater_id, "").strip() for debater_id in opp_debaters),
                roles_by_name=roles_by_name,
            )
        )
    return raw_rounds


def _parse_sql_insert_values(values_blob):
    rows = []
    current_row = []
    current_value = []
    in_string = False
    escaped = False
    inside_row = False

    for char in values_blob:
        if not inside_row:
            if char == "(":
                inside_row = True
                current_row = []
                current_value = []
            continue

        if in_string:
            current_value.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "'":
                in_string = False
            continue

        if char == "'":
            in_string = True
            current_value.append(char)
            continue
        if char == ",":
            current_row.append(_parse_sql_scalar("".join(current_value).strip()))
            current_value = []
            continue
        if char == ")":
            current_row.append(_parse_sql_scalar("".join(current_value).strip()))
            rows.append(current_row)
            current_row = []
            current_value = []
            inside_row = False
            continue
        current_value.append(char)
    return rows


def _parse_sql_scalar(raw_value):
    if raw_value == "NULL":
        return None
    if raw_value.startswith("'") and raw_value.endswith("'"):
        return _sql_unescape(raw_value[1:-1])
    return raw_value


def _sql_unescape(value):
    return (
        value.replace("\\\\", "\\")
        .replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
    )


def _extract_endpoint(original_file_name):
    match = re.search(r"(\d{8,})", str(original_file_name or ""))
    if not match:
        return None
    return match.group(1)


def _match_raw_round(round_obj, metadata_row, raw_rounds):
    gov_participants = _metadata_side_participants(metadata_row, "gov")
    opp_participants = _metadata_side_participants(metadata_row, "opp")
    candidates = []
    for raw_round in raw_rounds:
        if raw_round.round_number != int(round_obj.round_number or 0):
            continue
        if _map_source_names_to_participants(raw_round.gov_names, gov_participants) is None:
            continue
        if _map_source_names_to_participants(raw_round.opp_names, opp_participants) is None:
            continue
        candidates.append(raw_round)
    if len(candidates) != 1:
        return None
    return candidates[0]


def _side_participants(team):
    participants = []
    for debater in team.debaters.all():
        normalized_names = {_normalize_name(debater.name)}
        normalized_names.update(_normalize_name(alias.source_name) for alias in debater.aliases.all())
        participants.append(
            _Participant(
                debater_id=int(debater.id),
                display_name=str(debater.name or "").strip(),
                normalized_names=frozenset(name for name in normalized_names if name),
            )
        )
    return participants


def _metadata_side_participants(metadata_row, prefix):
    slot_names = ("gov_1", "gov_2") if prefix == "gov" else ("opp_1", "opp_2")
    participants = []
    seen_debater_ids = set()
    for slot_name in slot_names:
        alias = getattr(metadata_row, f"{slot_name}_alias", None)
        if alias is None or alias.debater_id in seen_debater_ids:
            continue
        seen_debater_ids.add(alias.debater_id)
        normalized_names = {
            _normalize_name(alias.source_name),
            _normalize_name(alias.debater.name),
        }
        normalized_names.update(
            _normalize_name(candidate_alias.source_name)
            for candidate_alias in alias.debater.aliases.all()
        )
        participants.append(
            _Participant(
                debater_id=int(alias.debater_id),
                display_name=str(alias.debater.name or "").strip(),
                normalized_names=frozenset(name for name in normalized_names if name),
            )
        )
    if participants:
        return participants
    return _side_participants(metadata_row.round.gov if prefix == "gov" else metadata_row.round.opp)


def _map_source_names_to_participants(source_names, participants):
    if len(source_names) != len(participants):
        return None
    normalized_source_names = [_normalize_name(name) for name in source_names]
    matches = []
    used_debater_ids = set()
    for normalized_name in normalized_source_names:
        candidates = [
            participant
            for participant in participants
            if normalized_name in participant.normalized_names and participant.debater_id not in used_debater_ids
        ]
        if len(candidates) != 1:
            return None
        participant = candidates[0]
        used_debater_ids.add(participant.debater_id)
        matches.append(participant)
    return matches


def _map_backup_roles_to_current_debaters(metadata_row, raw_round):
    desired = {}
    gov_matches = _map_source_names_to_participants(
        raw_round.gov_names,
        _metadata_side_participants(metadata_row, "gov"),
    )
    opp_matches = _map_source_names_to_participants(
        raw_round.opp_names,
        _metadata_side_participants(metadata_row, "opp"),
    )
    if gov_matches is None or opp_matches is None:
        return None

    for raw_name, participant in zip(raw_round.gov_names, gov_matches):
        role = raw_round.roles_by_name.get(_normalize_name(raw_name))
        if role not in {"PM", "MG"}:
            return None
        desired[int(participant.debater_id)] = role
    for raw_name, participant in zip(raw_round.opp_names, opp_matches):
        role = raw_round.roles_by_name.get(_normalize_name(raw_name))
        if role not in {"LO", "MO"}:
            return None
        desired[int(participant.debater_id)] = role

    if set(desired.values()) != {"PM", "MG", "LO", "MO"}:
        return None
    return desired


def _build_update_action(round_obj, metadata_row, desired_roles):
    imported_metadata_payload = {}
    slot_changed = False
    current_slot_debater_ids = {
        slot: (
            getattr(metadata_row, f"{slot}_alias").debater_id
            if getattr(metadata_row, f"{slot}_alias") is not None
            else None
        )
        for slot in SPEAKER_SLOTS
    }
    for role, slot in ROLE_TO_SLOT.items():
        debater_id = next(
            (candidate_debater_id for candidate_debater_id, candidate_role in desired_roles.items() if candidate_role == role),
            None,
        )
        if debater_id is None:
            return None
        debater = Debater.all_objects.get(pk=debater_id)
        imported_metadata_payload[slot] = {
            "debater_id": int(debater_id),
            "source_name": str(debater.name or "").strip(),
            "role": role,
        }
        if current_slot_debater_ids.get(slot) != int(debater_id):
            slot_changed = True

    stats_payload = []
    stat_role_changed = False
    for stat in round_obj.stats.all().order_by("score_index", "id"):
        updated_role = desired_roles.get(int(stat.debater_id), stat.debater_role)
        if (stat.debater_role or "") != (updated_role or ""):
            stat_role_changed = True
        stats_payload.append(
            {
                "debater_id": int(stat.debater_id),
                "speaks": _json_safe_number(stat.speaks),
                "ranks": _json_safe_number(stat.ranks),
                "debater_role": updated_role,
                "score_index": int(stat.score_index or 1),
                "source_status": str(stat.source_status or ""),
                "metadata": stat.metadata or {},
            }
        )

    if not slot_changed and not stat_role_changed:
        return None

    action = {
        "type": "update_round",
        "tournament_id": int(round_obj.tournament_id),
        "import_key": str(round_obj.import_key or ""),
    }
    if slot_changed:
        action["imported_metadata"] = imported_metadata_payload
    if stat_role_changed:
        action["stats"] = stats_payload
    return action


def _report_row(round_obj, metadata_row, *, status, import_row=None, source_path=""):
    return {
        "status": status,
        "round_id": int(round_obj.id),
        "tournament_id": int(round_obj.tournament_id),
        "tournament_name": str(round_obj.tournament.manual_name or round_obj.tournament.name or ""),
        "round_label": str(round_obj.round_label or ""),
        "round_number": int(round_obj.round_number or 0),
        "import_key": str(round_obj.import_key or ""),
        "source_import_id": int(import_row.id) if import_row is not None else None,
        "source_file_name": str(import_row.original_file_name or "") if import_row is not None else "",
        "source_path": str(source_path or ""),
        "current_slots": {
            slot: (
                getattr(metadata_row, f"{slot}_alias").debater.name
                if getattr(metadata_row, f"{slot}_alias") is not None
                else None
            )
            for slot in SPEAKER_SLOTS
        },
    }


def _canonical_role(value):
    normalized = str(value or "").strip().upper()
    if normalized in {"PM", "MG", "LO", "MO"}:
        return normalized
    return None


def _coerce_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_name(value):
    value = str(value or "").strip().casefold()
    collapsed = " ".join(value.split())
    return "".join(char for char in collapsed if char.isalnum() or char == " ")


def _json_safe_number(value):
    if value is None:
        return None
    return str(value)


def _timestamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
