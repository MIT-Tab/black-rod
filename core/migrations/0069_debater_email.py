from django.db import migrations, models


def inherit_claimed_user_emails(apps, _schema_editor):
    Debater = apps.get_model("core", "Debater")
    claimed_debaters = (
        Debater.objects.filter(user__isnull=False)
        .select_related("user")
        .only("id", "email", "user__email")
    )
    for debater in claimed_debaters.iterator():
        if debater.email:
            continue
        email = (debater.user.email or "").strip()
        if email:
            debater.email = email
            debater.save(update_fields=["email"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0068_generatedcode"),
    ]

    operations = [
        migrations.AddField(
            model_name="debater",
            name="email",
            field=models.EmailField(
                blank=True,
                help_text="Private contact email for tournament registration prefill.",
                max_length=254,
                null=True,
            ),
        ),
        migrations.RunPython(inherit_claimed_user_emails, migrations.RunPython.noop),
    ]
