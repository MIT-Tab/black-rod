from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0064_remove_round_is_rated_remove_round_weight"),
    ]

    operations = [
        migrations.AddField(
            model_name="syntheticresolutionlog",
            name="action",
            field=models.CharField(
                choices=[("resolved", "Resolved"), ("rejected", "Rejected")],
                db_index=True,
                default="resolved",
                max_length=16,
            ),
        ),
    ]
