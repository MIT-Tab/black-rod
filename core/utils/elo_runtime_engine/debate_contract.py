"""Extracts canonical winner, stage, weight, sort, and source labels from Round rows with minimal import-aware fallbacks."""


from datetime import datetime, timezone
from django.core.exceptions import ObjectDoesNotExist

from core.models import Round
from core.utils.elo_runtime_engine.constants import to_int


def _metadata_dict(debate):
    return debate.metadata if isinstance(getattr(debate, "metadata", None), dict) else {}


def winner_code_for_debate(debate):
    metadata = _metadata_dict(debate)
    from_metadata = str(metadata.get("winner") or "").strip().lower()
    if from_metadata in {"a", "b"}:
        return from_metadata

    victor = to_int(getattr(debate, "victor", None))
    if victor in {Round.GOV, Round.GOV_VIA_FORFEIT}:
        return "a"
    if victor in {Round.OPP, Round.OPP_VIA_FORFEIT}:
        return "b"
    return ""


def is_rated_debate(debate):
    victor = to_int(getattr(debate, "victor", None))
    if victor in {
        Round.GOV_VIA_FORFEIT,
        Round.OPP_VIA_FORFEIT,
        Round.ALL_DROP,
        Round.ALL_WIN,
        Round.BYE,
    }:
        return False

    metadata = _metadata_dict(debate)
    if "is_rated" in metadata:
        return bool(metadata.get("is_rated"))
    return bool(getattr(debate, "is_rated", False))


def debate_round_label(debate):
    metadata = _metadata_dict(debate)
    metadata_round = str(metadata.get("round_label") or "").strip()
    if metadata_round:
        return metadata_round

    direct_round = str(getattr(debate, "round_label", "") or "").strip()
    if direct_round:
        return direct_round

    sequence_number = to_int(getattr(debate, "sequence_number", None))
    if sequence_number is None:
        sequence_number = to_int(getattr(debate, "round_number", None))

    stage_value = stage_for_rating(debate)
    if stage_value == "outround":
        return "E%s" % (sequence_number if sequence_number is not None else "?")
    return "P%s" % (sequence_number if sequence_number is not None else "?")


def debate_weight(debate):
    metadata = _metadata_dict(debate)
    raw_weight = metadata.get("weight", getattr(debate, "weight", 1.0))
    try:
        weight = float(raw_weight)
    except (TypeError, ValueError):
        return 1.0
    return weight if weight > 0 else 1.0


def debate_sort_key(debate):
    metadata = _metadata_dict(debate)
    raw_values = metadata.get("sort_key")
    if isinstance(raw_values, (list, tuple)) and len(raw_values) == 3:
        values = []
        for raw_value in raw_values:
            value = to_int(raw_value)
            if value is None:
                values = []
                break
            values.append(int(value))
        if len(values) == 3:
            return tuple(values)

    stage = stage_for_rating(debate)
    round_number = to_int(getattr(debate, "round_number", None)) or 0
    return (0 if stage != "outround" else 1, round_number, int(getattr(debate, "id", 0) or 0))


def debate_source_fields(debate):
    source_kind = ""
    source_label = ""

    try:
        imported_metadata = debate.imported_metadata
    except ObjectDoesNotExist:
        imported_metadata = None

    if imported_metadata is not None:
        source_rows = getattr(imported_metadata, "ordered_sources", None)
        if source_rows is None:
            source_rows = list(imported_metadata.sources.all().order_by("id"))
        if source_rows:
            primary_source = source_rows[0]
            source_kind = str(primary_source.import_type or "").strip() or "round"
            source_label = (
                str(primary_source.original_file_name or "").strip()
                or "%s:%s" % (source_kind, int(primary_source.id))
            )

    if not source_kind:
        source_kind = str(getattr(debate, "import_origin", "") or "").strip() or "round"
    if not source_label:
        source_label = "%s:%s" % (source_kind, int(getattr(debate, "id", 0) or 0))
    return source_kind, source_label


def debate_timestamp(debate, fallback_timestamp):
    metadata = _metadata_dict(debate)
    raw_value = str(metadata.get("timestamp") or "").strip()
    if not raw_value:
        return fallback_timestamp
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return fallback_timestamp
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def debate_tournament_key(debate, fallback_key):
    metadata = _metadata_dict(debate)
    value = str(metadata.get("tournament_key") or "").strip()
    return value or fallback_key


def debate_tournament_name(debate, fallback_name):
    metadata = _metadata_dict(debate)
    value = str(metadata.get("tournament_name") or "").strip()
    return value or fallback_name


def metadata_team_ids(metadata, key):
    raw_values = metadata.get(key)
    if not isinstance(raw_values, (list, tuple)):
        return ()
    values = []
    for raw_value in raw_values:
        value = to_int(raw_value)
        if value is None:
            return ()
        values.append(int(value))
    if not values:
        return ()
    return tuple(values)


def metadata_team_names(metadata, key):
    raw_values = metadata.get(key)
    if not isinstance(raw_values, (list, tuple)):
        return ()
    return tuple(str(raw_value or "").strip() for raw_value in raw_values)


def stage_for_rating(debate):
    metadata = _metadata_dict(debate)
    metadata_stage = str(metadata.get("stage") or "").strip().lower()
    if metadata_stage in {"prelim", "outround"}:
        return metadata_stage

    raw_stage = str(getattr(debate, "stage", "") or "").strip().lower()
    if raw_stage in {"outround", "elim"}:
        return "outround"
    return "prelim"
