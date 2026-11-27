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


if __name__ == "__main__":
    main()
