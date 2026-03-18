from collections import Counter, defaultdict
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

SYNTHETIC_ENTITY_TYPES_BY_MODEL = {
    model: entity_type for entity_type, model in SYNTHETIC_ENTITY_MODELS.items()
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


@dataclass(frozen=True)
class SyntheticCleanupAnalysis:
    internal_references: dict
    external_reference_counts: dict
    isolation_external_counts: dict
    component_blocker_counts: dict
    directly_unreferenced_tokens: frozenset
    eligible_tokens: frozenset


def synthetic_cleanup_analysis():
    synthetic_ids_by_model = {}
    synthetic_objects_by_type = {}

    for entity_type, model in SYNTHETIC_ENTITY_MODELS.items():
        queryset = _manager_for_model(model).filter(synthetic=True).order_by("-id")
        synthetic_objects_by_type[entity_type] = list(queryset)
        synthetic_ids_by_model[model] = {
            int(obj.pk)
            for obj in synthetic_objects_by_type[entity_type]
        }

    internal_references = defaultdict(list)
    external_reference_counts = defaultdict(Counter)
    isolation_external_counts = defaultdict(Counter)

    for entity_type, model in SYNTHETIC_ENTITY_MODELS.items():
        synthetic_ids = synthetic_ids_by_model[model]
        if not synthetic_ids:
            continue

        for relation in model._meta.related_objects:
            _record_related_object_references(
                entity_type=entity_type,
                relation=relation,
                synthetic_ids=synthetic_ids,
                synthetic_ids_by_model=synthetic_ids_by_model,
                internal_references=internal_references,
                external_reference_counts=external_reference_counts,
            )

        for field in model._meta.many_to_many:
            _record_direct_many_to_many_references(
                entity_type=entity_type,
                field=field,
                synthetic_ids=synthetic_ids,
                synthetic_ids_by_model=synthetic_ids_by_model,
                internal_references=internal_references,
                external_reference_counts=external_reference_counts,
            )

        for field in model._meta.fields:
            if not getattr(field, "is_relation", False) or field.auto_created:
                continue
            _record_direct_relation_isolation_references(
                entity_type=entity_type,
                field=field,
                synthetic_ids=synthetic_ids,
                synthetic_ids_by_model=synthetic_ids_by_model,
                isolation_external_counts=isolation_external_counts,
            )

    directly_unreferenced_tokens = _directly_unreferenced_tokens(
        synthetic_objects_by_type=synthetic_objects_by_type,
        internal_references=internal_references,
        external_reference_counts=external_reference_counts,
    )
    eligible_tokens = _eligible_cleanup_tokens(
        synthetic_objects_by_type=synthetic_objects_by_type,
        internal_references=internal_references,
        external_reference_counts=external_reference_counts,
        isolation_external_counts=isolation_external_counts,
    )
    eligible_tokens.update(directly_unreferenced_tokens)
    component_blocker_counts = _component_blocker_counts(
        synthetic_objects_by_type=synthetic_objects_by_type,
        internal_references=internal_references,
        external_reference_counts=external_reference_counts,
        isolation_external_counts=isolation_external_counts,
    )

    return SyntheticCleanupAnalysis(
        internal_references=dict(internal_references),
        external_reference_counts={
            token: dict(counts)
            for token, counts in external_reference_counts.items()
        },
        isolation_external_counts={
            token: dict(counts)
            for token, counts in isolation_external_counts.items()
        },
        component_blocker_counts=component_blocker_counts,
        directly_unreferenced_tokens=frozenset(directly_unreferenced_tokens),
        eligible_tokens=frozenset(eligible_tokens),
    )


def synthetic_cleanup_sections():
    analysis = synthetic_cleanup_analysis()
    sections = []
    for entity_type, model in SYNTHETIC_ENTITY_MODELS.items():
        rows = []
        for obj in _manager_for_model(model).filter(synthetic=True).order_by("-id"):
            if _selection_token(entity_type, obj.pk) not in analysis.eligible_tokens:
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


def synthetic_entity_reference_summary(obj, analysis=None, ignored_selection_tokens=None):
    analysis = analysis or synthetic_cleanup_analysis()
    token = _selection_token_for_object(obj)
    counts = Counter(analysis.external_reference_counts.get(token, {}))

    ignored_selection_tokens = set(ignored_selection_tokens or [])
    for label, reference_token in analysis.internal_references.get(token, []):
        if reference_token in ignored_selection_tokens:
            continue
        counts[label] += 1

    return [
        {
            "label": label,
            "count": int(count),
        }
        for label, count in sorted(counts.items())
        if count
    ]


def synthetic_entity_is_unreferenced(obj, analysis=None, ignored_selection_tokens=None):
    return not synthetic_entity_reference_summary(
        obj,
        analysis=analysis,
        ignored_selection_tokens=ignored_selection_tokens,
    )


def synthetic_entity_cleanup_blocker_summary(obj, analysis=None, ignored_selection_tokens=None):
    analysis = analysis or synthetic_cleanup_analysis()
    token = _selection_token_for_object(obj)
    if token in analysis.directly_unreferenced_tokens:
        return []

    counts = Counter(
        {
            item["label"]: item["count"]
            for item in synthetic_entity_reference_summary(
                obj,
                analysis=analysis,
                ignored_selection_tokens=ignored_selection_tokens,
            )
        }
    )
    counts.update(analysis.isolation_external_counts.get(token, {}))
    if not counts:
        counts.update(analysis.component_blocker_counts.get(token, {}))
    return [
        {
            "label": label,
            "count": int(count),
        }
        for label, count in sorted(counts.items())
        if count
    ]


def synthetic_entity_is_cleanup_deletable(obj, analysis=None, ignored_selection_tokens=None):
    return not synthetic_entity_cleanup_blocker_summary(
        obj,
        analysis=analysis,
        ignored_selection_tokens=ignored_selection_tokens,
    )


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


def _directly_unreferenced_tokens(synthetic_objects_by_type, internal_references, external_reference_counts):
    return {
        _selection_token(entity_type, obj.pk)
        for entity_type, objects in synthetic_objects_by_type.items()
        for obj in objects
        if not internal_references.get(_selection_token(entity_type, obj.pk))
        and not external_reference_counts.get(_selection_token(entity_type, obj.pk))
    }


def _eligible_cleanup_tokens(
    synthetic_objects_by_type,
    internal_references,
    external_reference_counts,
    isolation_external_counts,
):
    tokens = {
        _selection_token(entity_type, obj.pk)
        for entity_type, objects in synthetic_objects_by_type.items()
        for obj in objects
    }
    adjacency = {token: set() for token in tokens}

    for token, references in internal_references.items():
        for _, reference_token in references:
            adjacency.setdefault(token, set()).add(reference_token)
            adjacency.setdefault(reference_token, set()).add(token)

    eligible_tokens = set()
    visited = set()

    for token in tokens:
        if token in visited:
            continue
        component = set()
        stack = [token]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            component.add(current)
            stack.extend(adjacency.get(current, ()) - visited)

        if any(
            external_reference_counts.get(component_token)
            or isolation_external_counts.get(component_token)
            for component_token in component
        ):
            continue
        eligible_tokens.update(component)

    return eligible_tokens


def _component_blocker_counts(
    *,
    synthetic_objects_by_type,
    internal_references,
    external_reference_counts,
    isolation_external_counts,
):
    tokens = {
        _selection_token(entity_type, obj.pk)
        for entity_type, objects in synthetic_objects_by_type.items()
        for obj in objects
    }
    adjacency = {token: set() for token in tokens}

    for token, references in internal_references.items():
        for _, reference_token in references:
            adjacency.setdefault(token, set()).add(reference_token)
            adjacency.setdefault(reference_token, set()).add(token)

    blocker_counts_by_token = {}
    visited = set()

    for token in tokens:
        if token in visited:
            continue
        component = set()
        stack = [token]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            component.add(current)
            stack.extend(adjacency.get(current, ()) - visited)

        component_counts = Counter()
        for component_token in component:
            component_counts.update(external_reference_counts.get(component_token, {}))
            component_counts.update(isolation_external_counts.get(component_token, {}))

        for component_token in component:
            blocker_counts_by_token[component_token] = dict(component_counts)

    return blocker_counts_by_token


def _record_related_object_references(
    *,
    entity_type,
    relation,
    synthetic_ids,
    synthetic_ids_by_model,
    internal_references,
    external_reference_counts,
):
    label = relation.get_accessor_name()
    source_model = relation.related_model

    if relation.many_to_many:
        through = _manager_for_model(relation.field.remote_field.through)
        current_field_name = relation.field.m2m_reverse_field_name()
        source_field_name = relation.field.m2m_field_name()
        rows = through.filter(**{f"{current_field_name}_id__in": synthetic_ids}).values_list(
            f"{current_field_name}_id",
            f"{source_field_name}_id",
        )
    else:
        rows = _manager_for_model(source_model).filter(
            **{f"{relation.field.attname}__in": synthetic_ids}
        ).values_list(
            relation.field.attname,
            "pk",
        )

    for object_id, source_id in rows:
        current_token = _selection_token(entity_type, object_id)
        reference_token = _synthetic_reference_token(
            model=source_model,
            object_id=source_id,
            synthetic_ids_by_model=synthetic_ids_by_model,
        )
        if reference_token is None:
            external_reference_counts[current_token][label] += 1
            continue
        internal_references[current_token].append((label, reference_token))


def _record_direct_many_to_many_references(
    *,
    entity_type,
    field,
    synthetic_ids,
    synthetic_ids_by_model,
    internal_references,
    external_reference_counts,
):
    through = _manager_for_model(field.remote_field.through)
    current_field_name = field.m2m_field_name()
    related_field_name = field.m2m_reverse_field_name()
    related_model = field.remote_field.model
    rows = through.filter(**{f"{current_field_name}_id__in": synthetic_ids}).values_list(
        f"{current_field_name}_id",
        f"{related_field_name}_id",
    )

    for object_id, related_id in rows:
        current_token = _selection_token(entity_type, object_id)
        reference_token = _synthetic_reference_token(
            model=related_model,
            object_id=related_id,
            synthetic_ids_by_model=synthetic_ids_by_model,
        )
        if reference_token is None:
            external_reference_counts[current_token][field.name] += 1
            continue
        internal_references[current_token].append((field.name, reference_token))


def _record_direct_relation_isolation_references(
    *,
    entity_type,
    field,
    synthetic_ids,
    synthetic_ids_by_model,
    isolation_external_counts,
):
    if not (getattr(field, "many_to_one", False) or getattr(field, "one_to_one", False)):
        return

    related_model = field.remote_field.model
    rows = _manager_for_model(field.model).filter(
        pk__in=synthetic_ids,
    ).exclude(
        **{field.attname: None}
    ).values_list(
        "pk",
        field.attname,
    )

    for object_id, related_id in rows:
        reference_token = _synthetic_reference_token(
            model=related_model,
            object_id=related_id,
            synthetic_ids_by_model=synthetic_ids_by_model,
        )
        if reference_token is not None:
            continue
        isolation_external_counts[_selection_token(entity_type, object_id)][field.name] += 1


def _selection_token_for_object(obj):
    entity_type = SYNTHETIC_ENTITY_TYPES_BY_MODEL[obj.__class__]
    return _selection_token(entity_type, obj.pk)


def _selection_token(entity_type, object_id):
    return f"{entity_type}:{int(object_id)}"


def _synthetic_reference_token(*, model, object_id, synthetic_ids_by_model):
    entity_type = SYNTHETIC_ENTITY_TYPES_BY_MODEL.get(model)
    if entity_type is None:
        return None
    if int(object_id) not in synthetic_ids_by_model.get(model, set()):
        return None
    return _selection_token(entity_type, object_id)


def _manager_for_model(model):
    return getattr(model, "all_objects", None) or model._base_manager
