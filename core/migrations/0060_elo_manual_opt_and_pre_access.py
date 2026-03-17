from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0059_result_counts_for_points"),
    ]

    operations = [
        migrations.AddField(
            model_name="debater",
            name="elo_manual_opt",
            field=models.CharField(
                blank=True,
                choices=[("", "Unset"), ("opt_in", "Opt-in"), ("opt_out", "Opt-out")],
                db_index=True,
                default="",
                max_length=8,
            ),
        ),
        migrations.AlterModelOptions(
            name="user",
            options={
                "permissions": (
                    (
                        "exclusive_pre_access",
                        "Can access exclusive pre-access ELO and speaks views",
                    ),
                ),
            },
        ),
    ]
