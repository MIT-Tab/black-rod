from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0075_immaculate_grid")]

    operations = [
        migrations.AddField(
            model_name="immaculategridboard",
            name="replaced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="immaculategridboard",
            name="date",
            field=models.DateField(db_index=True),
        ),
        migrations.AddConstraint(
            model_name="immaculategridboard",
            constraint=models.UniqueConstraint(
                fields=("date",), condition=models.Q(replaced_at__isnull=True),
                name="one_active_immaculate_board_per_day",
            ),
        ),
    ]
