from django.core.management.base import BaseCommand

from core.forum_publication import configured, sync_tab_cards
from core.models.tournament import Tournament


class Command(BaseCommand):
    help = "Retry sending tournament tab card links to the APDA Forum."

    def handle(self, *args, **options):
        if configured():
            pending = Tournament.objects.filter(
                forum_publication_id__isnull=False,
                forum_tab_cards_synced=False,
                rounds__isnull=False,
            ).values_list("pk", flat=True).distinct()
            for tournament_id in pending.iterator():
                sync_tab_cards(tournament_id)
