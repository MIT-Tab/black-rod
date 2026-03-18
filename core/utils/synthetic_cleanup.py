from dataclasses import dataclass

from core.models import Debater, School, Team


SYNTHETIC_ENTITY_MODELS = {
    "school": School,
    "debater": Debater,
    "team": Team,
}


SYNTHETIC_ENTITY_LABELS = {
    "school": "Schools",
    "debater": "Debaters",
    "team": "Teams",
}


@dataclass(frozen=True)
class SyntheticCleanupRow:
    entity_type: str
    object_id: int
    display_name: str
    secondary_text: str

    @property
    def selection_token(self):
        return f"{self.entity_type}:{self.object_id}"


def synthetic_cleanup_sections():
    sections = []
    for entity_type, model in SYNTHETIC_ENTITY_MODELS.items():
        referenced_ids = _referenced_object_ids(model)
        rows = []
        for obj in _manager_for_model(model).filter(synthetic=True).order_by("-id"):
            if int(obj.pk) in referenced_ids:
                continue
            rows.append(
                SyntheticCleanupRow(
                    entity_type=entity_type,
                    object_id=int(obj.pk),
                    display_name=_display_name(obj),
                    secondary_text=_secondary_text(entity_type, obj),
                )
            )
        sections.append(
            {
                "entity_type": entity_type,
                "label": SYNTHETIC_ENTITY_LABELS[entity_type],
                "rows": rows,
                "count": len(rows),
            }
        )
    return sections


def get_synthetic_entity(entity_type, object_id):
    model = SYNTHETIC_ENTITY_MODELS.get(str(entity_type or "").strip().lower())
    if model is None:
        return None
    try:
        return _manager_for_model(model).get(pk=int(object_id), synthetic=True)
    except (model.DoesNotExist, TypeError, ValueError):
        return None


def parse_selection_token(token):
    raw = str(token or "").strip()
    if ":" not in raw:
        return None, None
    entity_type, object_id = raw.split(":", 1)
    entity_type = entity_type.strip().lower()
    try:
        return entity_type, int(object_id)
    except (TypeError, ValueError):
        return None, None


def synthetic_entity_reference_summary(obj):
    references = []

    for relation in obj._meta.related_objects:
        count = _related_object_count(obj, relation)
        if count:
            references.append(
                {
                    "label": relation.get_accessor_name(),
                    "count": int(count),
                }
            )

    for field in obj._meta.many_to_many:
        count = _direct_many_to_many_count(obj, field)
        if count:
            references.append(
                {
                    "label": field.name,
                    "count": int(count),
                }
            )

    return references


def synthetic_entity_is_unreferenced(obj):
    return not synthetic_entity_reference_summary(obj)


def _display_name(obj):
    return str(getattr(obj, "display_name", None) or getattr(obj, "name", None) or obj)


def _secondary_text(entity_type, obj):
    if entity_type == "school":
        short_name = str(getattr(obj, "short_name", "") or "").strip()
        if short_name:
            return f"Short name: {short_name}"
        return "No short name"

    if entity_type == "debater":
        school = getattr(obj, "school", None)
        school_name = school.name if school else "Unaffiliated"
        return f"School: {school_name}"

    if entity_type == "team":
        return f"Short name: {str(getattr(obj, 'short_name', '') or '').strip() or 'None'}"

    return ""


def _referenced_object_ids(model):
    referenced_ids = set()

    for relation in model._meta.related_objects:
        referenced_ids.update(_referenced_ids_for_relation(model, relation))

    for field in model._meta.many_to_many:
        referenced_ids.update(_referenced_ids_for_direct_many_to_many(field))

    return referenced_ids


def _referenced_ids_for_relation(model, relation):
    if relation.many_to_many:
        through = _manager_for_model(relation.field.remote_field.through)
        current_field_name = relation.field.m2m_reverse_field_name()
        return {
            int(value)
            for value in through.values_list(f"{current_field_name}_id", flat=True).distinct()
            if value is not None
        }

    attname = relation.field.attname
    return {
        int(value)
        for value in _manager_for_model(relation.related_model).values_list(attname, flat=True).distinct()
        if value is not None
    }


def _referenced_ids_for_direct_many_to_many(field):
    through = _manager_for_model(field.remote_field.through)
    current_field_name = field.m2m_field_name()
    return {
        int(value)
        for value in through.values_list(f"{current_field_name}_id", flat=True).distinct()
        if value is not None
    }


def _related_object_count(obj, relation):
    if relation.many_to_many:
        through = _manager_for_model(relation.field.remote_field.through)
        current_field_name = relation.field.m2m_reverse_field_name()
        return through.filter(**{f"{current_field_name}_id": obj.pk}).count()

    return _manager_for_model(relation.related_model).filter(**{relation.field.attname: obj.pk}).count()


def _direct_many_to_many_count(obj, field):
    through = _manager_for_model(field.remote_field.through)
    current_field_name = field.m2m_field_name()
    return through.filter(**{f"{current_field_name}_id": obj.pk}).count()


def _manager_for_model(model):
    return getattr(model, "all_objects", None) or model._base_manager
