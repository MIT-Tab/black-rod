"""Export the existing Django foreign-key deletion rules for the native server."""
import json
from pathlib import Path

from django import setup
from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db.models.deletion import get_candidate_relations_to_delete


def row_order(opts):
    columns = []
    for ordering in opts.ordering:
        if not isinstance(ordering, str):
            continue
        key = ordering.removeprefix("-")
        try:
            field = opts.pk if key == "pk" else opts.get_field(key)
        except FieldDoesNotExist:
            continue
        if field.column:
            columns.append(f'"{field.column}" {"DESC" if ordering.startswith("-") else "ASC"}')
    return ",".join(columns) or "-"


def main():
    setup()
    rows = []
    labels = {}
    for model in sorted(apps.get_models(include_auto_created=True), key=lambda m: m._meta.db_table):
        opts = model._meta
        table = opts.db_table
        labels[table] = (opts.object_name, str(opts.verbose_name), str(opts.verbose_name_plural))
        for reverse in get_candidate_relations_to_delete(opts):
            field = reverse.field
            relation = field.remote_field
            parent = relation.model._meta
            child = field.model._meta
            action = relation.on_delete.__name__
            if child.pk.column != "id" or parent.pk.column != "id":
                raise ValueError(f"Deletion metadata expects numeric id fields in {table}")
            if action not in {"CASCADE", "PROTECT", "SET_NULL"}:
                raise ValueError(f"Unknown deletion action {action} in {table}")
            rows.append((table, child.db_table, field.column, action, "-", row_order(child)))
        for field in opts.private_fields:
            if hasattr(field, "bulk_related_objects"):
                child = field.related_model._meta
                object_column = child.get_field(field.object_id_field_name).column
                content_column = child.get_field(field.content_type_field_name).column
                rows.append((table, child.db_table, object_column, "CASCADE",
                             f"{content_column}:{opts.app_label}.{opts.model_name}", row_order(child)))
    target = Path(__file__).resolve().parents[1] / "schema" / "delete_relations.tsv"
    target.write_text("".join("\t".join(row) + "\n" for row in rows), encoding="utf-8")
    target.with_name("delete_labels.json").write_text(json.dumps(labels, indent=2) + "\n", encoding="utf-8")
    cleanup_models = [apps.get_model("core", name) for name in ("School", "Debater", "Team")]
    cleanup = []
    for model in cleanup_models:
        parent = model._meta.model_name
        for relation in model._meta.related_objects:
            source = relation.related_model
            if relation.many_to_many:
                field = relation.field
                through = field.remote_field.through._meta
                table = through.db_table
                current = through.get_field(field.m2m_reverse_field_name()).column
                related = through.get_field(field.m2m_field_name()).column
            else:
                table = source._meta.db_table
                current = relation.field.column
                related = source._meta.pk.column
            cleanup.append((parent, relation.get_accessor_name(), table, current, related,
                            source._meta.model_name if source in cleanup_models else "-", "inbound"))
        for field in model._meta.many_to_many:
            through = field.remote_field.through._meta
            source = field.remote_field.model
            cleanup.append((parent, field.name, through.db_table,
                            through.get_field(field.m2m_field_name()).column,
                            through.get_field(field.m2m_reverse_field_name()).column,
                            source._meta.model_name if source in cleanup_models else "-", "inbound"))
        for field in model._meta.fields:
            if field.is_relation and not field.auto_created and (field.many_to_one or field.one_to_one):
                source = field.remote_field.model
                cleanup.append((parent, field.name, model._meta.db_table, model._meta.pk.column,
                                field.column, source._meta.model_name if source in cleanup_models else "-", "outbound"))
    target.with_name("synthetic_cleanup.tsv").write_text(
        "".join("\t".join(row) + "\n" for row in sorted(cleanup)), encoding="utf-8",
    )


if __name__ == "__main__":
    main()
