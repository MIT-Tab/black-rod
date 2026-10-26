from django.core.management.base import BaseCommand

from core.import_sources import refresh_import_sources


class Command(BaseCommand):
    help = "Cache complete mit-tab snapshots. Schedule every 12 hours on the application host."

    def handle(self, *args, **options):
        sources = refresh_import_sources()
        self.stdout.write(f"Retained {len(sources)} tournament snapshots.")
