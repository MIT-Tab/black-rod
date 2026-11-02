"""Issue or revoke agent credentials from an administrator's server console."""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import AgentCredential, User


class Command(BaseCommand):
    help = "Issue an expiring, hashed agent credential or revoke one. Requires an active superuser."

    def add_arguments(self, parser):
        parser.add_argument("--admin", required=True, help="Active superuser username")
        parser.add_argument("--name", help="Agent name (required to issue)")
        parser.add_argument("--days", type=int, default=7)
        parser.add_argument(
            "--write", action="store_true", help="Allow bulk corrections and undo"
        )
        parser.add_argument("--revoke", type=int, metavar="CREDENTIAL_ID")

    def handle(self, *args, **options):
        admin = User.objects.filter(
            username=options["admin"], is_active=True, is_superuser=True
        ).first()
        if not admin:
            raise CommandError("An active superuser is required.")
        if options["revoke"] is not None:
            if not AgentCredential.objects.filter(pk=options["revoke"]).update(
                revoked_at=timezone.now()
            ):
                raise CommandError("Credential not found.")
            self.stdout.write("Credential revoked.")
            return
        if not options["name"] or not 1 <= options["days"] <= 90:
            raise CommandError("Provide --name and --days between 1 and 90.")
        credential, token = AgentCredential.issue(
            admin=admin,
            name=options["name"],
            expires_at=timezone.now() + timedelta(days=options["days"]),
            can_write=options["write"],
        )
        self.stdout.write(
            f"Credential {credential.pk}: copy this token now; only its hash is stored."
        )
        self.stdout.write(token)
