from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0065_syntheticresolutionlog_action"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="user",
            options={
                "permissions": (
                    (
                        "exclusive_pre_access",
                        "Can access exclusive pre-access ELO and speaks views",
                    ),
                    (
                        "can_view_debug_tab_cards",
                        "Can view CSVs for debug",
                    ),
                ),
            },
        ),
    ]
