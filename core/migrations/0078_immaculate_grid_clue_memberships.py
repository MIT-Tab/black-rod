from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0077_merge_taggit_and_grid_migrations")]

    operations = [
        migrations.AddField(
            model_name="immaculategridboard",
            name="clue_memberships",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
