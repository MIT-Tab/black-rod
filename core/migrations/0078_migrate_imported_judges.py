from django.db import migrations
from django.db.models import Q


def migrate_mittab_judges(apps, schema_editor):
    db = schema_editor.connection.alias
    Assignment = apps.get_model("core", "ImportedRoundJudge")
    Judge = apps.get_model("core", "Judge")
    Debater = apps.get_model("core", "Debater")
    Alias = apps.get_model("core", "DebaterAlias")
    ResolutionLog = apps.get_model("core", "SyntheticResolutionLog")

    assignments = Assignment.objects.using(db)
    mittab_assignments = assignments.filter(
        Q(round_metadata__round__import_origin="mittab_bundle")
        | Q(round_metadata__sources__import_type="mittab_bundle")
    ).distinct()
    candidates = set()
    for assignment in mittab_assignments.select_related(
        "round_metadata__round", "debater_alias__debater",
    ).iterator(chunk_size=1000):
        judge, _ = Judge.objects.using(db).get_or_create(
            source_tournament_id=assignment.round_metadata.round.tournament_id,
            source_key=f"name:{assignment.original_name}",
            defaults={"name": assignment.original_name},
        )
        assignments.filter(pk=assignment.pk).update(judge=judge)
        alias = assignment.debater_alias
        if alias and alias.debater.synthetic and not alias.debater.temporary and not alias.debater.school_id:
            candidates.add(alias.debater_id)

    for debater_id in candidates:
        if ResolutionLog.objects.using(db).filter(entity_type="debater").filter(
            Q(synthetic_id=debater_id) | Q(resolved_to_id=debater_id)
        ).exists():
            continue
        if _has_other_references(Debater, debater_id, db, excluded="aliases"):
            continue
        aliases = Alias.objects.using(db).filter(debater_id=debater_id)
        if any(_has_other_references(Alias, alias.pk, db, excluded="judged_rounds") for alias in aliases):
            continue
        linked_assignments = assignments.filter(debater_alias__debater_id=debater_id)
        unconverted_assignments = linked_assignments.filter(judge__isnull=True)
        if unconverted_assignments.exists() or aliases.exclude(judged_rounds__isnull=False).exists():
            continue
        linked_assignments.update(debater_alias=None)
        Debater._base_manager.using(db).filter(pk=debater_id).delete()  # pylint: disable=protected-access


def _has_other_references(model, pk, db, *, excluded):
    return any(
        relation.related_model._base_manager.using(db).filter(  # pylint: disable=protected-access
            **{relation.field.name: pk}
        ).exists()
        for relation in model._meta.related_objects  # pylint: disable=protected-access
        if relation.get_accessor_name() != excluded
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0077_tournament_tab_cards")]

    # The old schema still has original_name on each assignment after a rollback.
    operations = [migrations.RunPython(migrate_mittab_judges, migrations.RunPython.noop)]
