"""Record the correction API's explicit field allowlist and revision schema."""
import json
from pathlib import Path

from django import setup


def main():
    setup()
    from core.utils.agent_data import READ_ONLY, RESOURCES, fields_for, m2m_for, normalized, resource_model

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
                }
                for field in model._meta.local_many_to_many
            },
        }
    path = Path(__file__).resolve().parents[1] / "schema" / "agent_resources.json"
    path.write_text(json.dumps(normalized(result), ensure_ascii=True, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")


if __name__ == "__main__":
    main()
