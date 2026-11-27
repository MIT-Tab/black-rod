"""Export the existing Django foreign-key deletion rules for the native server."""
from pathlib import Path

from django import setup
from django.apps import apps
from django.db.models import ForeignKey


def main():
    setup()
    rows = []
    for model in apps.get_models(include_auto_created=True):
        table = model._meta.db_table
        for field in model._meta.local_fields:
            if not isinstance(field, ForeignKey):
                continue
            relation = field.remote_field
            parent = relation.model._meta
            action = relation.on_delete.__name__
            if model._meta.pk.column != "id" or parent.pk.column != "id":
                raise ValueError(f"Deletion metadata expects numeric id fields in {table}")
            if action not in {"CASCADE", "PROTECT", "SET_NULL"}:
                raise ValueError(f"Unknown deletion action {action} in {table}")
            rows.append((parent.db_table, table, field.column, action))
    target = Path(__file__).resolve().parents[1] / "schema" / "delete_relations.tsv"
    target.write_text("".join("\t".join(row) + "\n" for row in sorted(rows)), encoding="utf-8")
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
