"""Record the correction API's explicit field allowlist and revision schema."""
import json
from pathlib import Path

from django import setup
from django.utils import timezone


def main():
    setup()
    from core.utils.agent_data import READ_ONLY, RESOURCES, all_core_models, fields_for, m2m_for, normalized, resource_model

    result = {}
    for name in sorted(RESOURCES.keys() | READ_ONLY.keys()):
        model = resource_model(name)
        exposed = {field.attname for field in fields_for(name)}
        editable = {field.attname for field in fields_for(name, writable=True)} if name in RESOURCES else set()
        memberships = {field.name for field in m2m_for(name)}
        fields = {}
        for field in model._meta.local_concrete_fields:
            fields[field.attname] = {
                "column": field.column,
                "exposed": field.attname in exposed,
                "scale": getattr(field, "decimal_places", None),
                "max_length": field.max_length,
                "default": (
                    None if field.default is timezone.now or getattr(field, "auto_now", False)
                    or getattr(field, "auto_now_add", False) else field.get_default()
                ),
                "auto_now": getattr(field, "auto_now", False),
                "auto_now_add": getattr(field, "auto_now_add", False),
                "schema": {
                    "type": field.get_internal_type(),
                    "nullable": field.null,
                    "blank": field.blank,
                    "writable": field.attname in editable,
                    "required_on_create": not (
                        field.has_default() or field.null or field.blank or field.primary_key or not field.editable
                    ),
                    "choices": list(field.choices) if field.choices else None,
                    "related_model": field.related_model._meta.label if field.is_relation else None,
                },
            }
            if fields[field.attname]["scale"] is not None:
                fields[field.attname]["max_digits"] = field.max_digits
        result[name] = {
            "table": model._meta.db_table,
            "label": model._meta.label,
            "writable": name in RESOURCES,
            "fields": fields,
            "memberships": {
                field.name: {
                    "table": field.remote_field.through._meta.db_table,
                    "label": field.remote_field.through._meta.label,
                    "source": field.m2m_field_name() + "_id",
                    "target": field.m2m_reverse_field_name() + "_id",
                    "exposed": field.name in memberships,
                    "related_table": field.related_model._meta.db_table,
                }
                for field in model._meta.local_many_to_many
            },
        }
    folder = Path(__file__).resolve().parents[1] / "schema"
    storage = {}
    for model in all_core_models():
        label = model._meta.label
        storage[label] = {
            "table": model._meta.db_table,
            "fields": {
                field.attname: {
                    "column": field.column,
                    "exposed": False,
                    "scale": getattr(field, "decimal_places", None),
                    "schema": {"type": field.get_internal_type()},
                }
                for field in model._meta.concrete_fields
            },
        }
    (folder / "agent_storage.json").write_text(
        "{\n" + ",\n".join(
            f"  {json.dumps(label)}: {json.dumps(table, sort_keys=True, separators=(',', ':'))}"
            for label, table in sorted(storage.items())
        ) + "\n}\n", encoding="utf-8"
    )
    path = folder / "agent_resources.json"
    path.write_text(json.dumps(normalized(result), ensure_ascii=True, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")


if __name__ == "__main__":
    main()
