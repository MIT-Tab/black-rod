from django.db import migrations, models


def sync_roundstats_stage_and_clear_outrounds(apps, schema_editor):
    DebaterAlias = apps.get_model("core", "DebaterAlias")
    ImportedRoundMetadata = apps.get_model("core", "ImportedRoundMetadata")
    RoundStats = apps.get_model("core", "RoundStats")

    RoundStats.objects.filter(round__stage="outround").update(
        stage="outround",
        speaks=None,
        ranks=None,
        debater_role=None,
    )
    RoundStats.objects.exclude(round__stage="outround").update(stage="prelim")

    alias_cache = {}
    slot_fields = (
        ("gov_1_alias_id", "gov_1_role"),
        ("gov_2_alias_id", "gov_2_role"),
        ("opp_1_alias_id", "opp_1_role"),
        ("opp_2_alias_id", "opp_2_role"),
    )

    for metadata_row in ImportedRoundMetadata.objects.iterator():
        for alias_id_field, role_field in slot_fields:
            alias_id = getattr(metadata_row, alias_id_field)
            role = str(getattr(metadata_row, role_field) or "").strip()
            if not alias_id or not role:
                continue

            debater_id = alias_cache.get(alias_id)
            if debater_id is None:
                debater_id = (
                    DebaterAlias.objects.filter(pk=alias_id)
                    .values_list("debater_id", flat=True)
                    .first()
                )
                alias_cache[alias_id] = debater_id
            if debater_id is None:
                continue

            matching_stats = RoundStats.objects.filter(
                round_id=metadata_row.round_id,
                debater_id=debater_id,
                stage="prelim",
            )
            if matching_stats.exclude(debater_role__isnull=True).exclude(debater_role="").exists():
                continue
            matching_stats.filter(debater_role__isnull=True).update(debater_role=role)
            matching_stats.filter(debater_role="").update(debater_role=role)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0062_schedulerworkspace_schedulingrun"),
    ]

    operations = [
        migrations.AddField(
            model_name="roundstats",
            name="stage",
            field=models.CharField(
                choices=[("prelim", "Prelim"), ("outround", "Outround")],
                default="prelim",
                max_length=16,
            ),
        ),
        migrations.RunPython(
            sync_roundstats_stage_and_clear_outrounds,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="roundstats",
            constraint=models.CheckConstraint(
                check=(
                    ~models.Q(stage="outround")
                    | (
                        models.Q(speaks__isnull=True)
                        & models.Q(ranks__isnull=True)
                        & (
                            models.Q(debater_role__isnull=True)
                            | models.Q(debater_role="")
                        )
                    )
                ),
                name="roundstats_outround_requires_blank_scores",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="importedroundmetadata",
            name="imported_round_metadata_distinct_gov_roles",
        ),
        migrations.RemoveConstraint(
            model_name="importedroundmetadata",
            name="imported_round_metadata_distinct_opp_roles",
        ),
        migrations.RemoveField(
            model_name="importedroundmetadata",
            name="gov_1_role",
        ),
        migrations.RemoveField(
            model_name="importedroundmetadata",
            name="gov_2_role",
        ),
        migrations.RemoveField(
            model_name="importedroundmetadata",
            name="opp_1_role",
        ),
        migrations.RemoveField(
            model_name="importedroundmetadata",
            name="opp_2_role",
        ),
    ]
