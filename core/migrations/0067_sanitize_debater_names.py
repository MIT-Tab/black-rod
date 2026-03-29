from django.db import migrations
from django.utils.html import strip_tags


def _sanitize_name_part(value, fallback=""):
    cleaned = strip_tags(str(value or "")).strip()
    return cleaned or fallback


def sanitize_debater_names(apps, schema_editor):
    Debater = apps.get_model("core", "Debater")
    debaters_to_update = []
    fallback_hits = []

    for debater in Debater._base_manager.all().iterator():
        original_first_name = str(debater.first_name or "")
        original_last_name = str(debater.last_name or "")
        stripped_first_name = _sanitize_name_part(debater.first_name)
        sanitized_first_name = stripped_first_name or "Removed"
        sanitized_last_name = _sanitize_name_part(debater.last_name)

        if (
            sanitized_first_name == debater.first_name
            and sanitized_last_name == debater.last_name
        ):
            continue

        if not stripped_first_name:
            fallback_hits.append(
                (
                    debater.pk,
                    original_first_name,
                    original_last_name,
                )
            )

        debater.first_name = sanitized_first_name
        debater.last_name = sanitized_last_name
        debaters_to_update.append(debater)

    if debaters_to_update:
        Debater._base_manager.bulk_update(
            debaters_to_update,
            ["first_name", "last_name"],
        )

    for debater_id, original_first_name, original_last_name in fallback_hits:
        print(
            "[0067_sanitize_debater_names] "
            f"fallback applied to debater id={debater_id} "
            f"original_first_name={original_first_name!r} "
            f"original_last_name={original_last_name!r}"
        )

    if fallback_hits:
        print(
            "[0067_sanitize_debater_names] "
            f"fallback applied to {len(fallback_hits)} debater name(s)"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0066_user_can_view_debug_tab_cards"),
    ]

    operations = [
        migrations.RunPython(sanitize_debater_names, migrations.RunPython.noop),
    ]
