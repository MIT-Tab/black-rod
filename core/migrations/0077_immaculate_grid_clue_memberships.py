from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0075_align_taggit_slug_fields"),
        ("core", "0076_immaculate_grid_replacements"),
    ]

    operations = [
        migrations.AddField(
            model_name="immaculategridboard",
            name="clue_memberships",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
