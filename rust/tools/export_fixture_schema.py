"""Record model and relationship metadata used by the native fixture exporter."""
from pathlib import Path
import json

from django import setup


def model_schema(model, explicit_pks):
    opts = model._meta
    fields = {}
    storage = {}
    for field in opts.local_fields:
        if not field.serialize and not field.primary_key:
            continue
        fields[field.name] = {
            "column": field.column,
            "related": field.related_model._meta.label_lower if field.is_relation else None,
            "primary": field.primary_key,
        }
        storage[field.column] = {
            "column": field.column,
            "exposed": False,
            "scale": getattr(field, "decimal_places", None),
            "schema": {"type": field.get_internal_type()},
        }
    memberships = {}
    for field in opts.local_many_to_many:
        through = field.remote_field.through._meta
        if not field.serialize or not through.auto_created:
            continue
        memberships[field.name] = {
            "through": [through.db_table, through.get_field(field.m2m_field_name()).column,
                        through.get_field(field.m2m_reverse_field_name()).column],
            "related": field.related_model._meta.label_lower,
        }
    ordering = []
    for name in opts.ordering:
        key = name.removeprefix("-")
        if "__" not in key:
            field = opts.pk if key == "pk" else opts.get_field(key)
            ordering.append([field.column, name.startswith("-")])
    return {
        "model": opts.label_lower,
        "title": model.__name__,
        "natural": hasattr(model, "natural_key") and opts.label_lower not in explicit_pks,
        "fields": fields,
        "memberships": memberships,
        "ordering": ordering,
        "storage": {"table": opts.db_table, "fields": storage},
    }


def main():
    setup()
    from django.apps import apps
    from django.contrib.auth.models import Group, Permission
    from django.contrib.contenttypes.models import ContentType
    from taggit.models import Tag, TaggedItem
    from core.management.commands.create_dev_fixtures import FIXTURE_EXPLICIT_PKS

    user = apps.get_model("core.User")
    video = apps.get_model("core.Video")
    models = [ContentType, Permission, Group, Tag, TaggedItem, user]
    models.extend(sorted(
        (model for model in apps.get_app_config("core").get_models() if model not in (user, video)),
        key=lambda model: model._meta.label_lower,
    ))
    models.append(video)
    output = Path(__file__).resolve().parents[1] / "schema" / "fixture.json"
    lines = [json.dumps(model_schema(model, FIXTURE_EXPLICIT_PKS), separators=(",", ":")) for model in models]
    output.write_text("[\n" + ",\n".join(lines) + "\n]\n", encoding="utf-8")


if __name__ == "__main__":
    main()
