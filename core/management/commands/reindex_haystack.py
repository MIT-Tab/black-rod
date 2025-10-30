from __future__ import print_function

from django.core.management.base import BaseCommand
from django.core import management
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Rebuild Haystack indexes (wrapper around `update_index`)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--using",
            dest="using",
            help="Specify the Haystack connection to use (default: 'default')",
            default="default",
        )
        parser.add_argument(
            "--remove-old",
            action="store_true",
            dest="remove_old",
            help="Clear existing index files before indexing (useful for Whoosh).",
        )

    # Use BaseCommand's built-in --verbosity option instead of adding a new one

    def handle(self, *args, **options):
        using = options.get("using")
        remove_old = options.get("remove_old")
        # BaseCommand already provides `verbosity` in options
        verbosity = options.get("verbosity")

        if remove_old:
            # For simple backends like Whoosh it's safest to clear before rebuilding.
            try:
                from haystack.utils import loading
                from haystack import connections

                ui = connections[using].get_unified_index()
                backend = connections[using].get_backend()
                if hasattr(backend, "remove"):
                    backend.remove(using=using)
            except Exception:
                # Best-effort; continuing to update_index below
                logger.exception("Failed to remove old index files; continuing to update_index")

        self.stdout.write("Starting haystack update_index (using=%s)" % using)

        try:
            # Only pass verbosity if it's not None to avoid duplicate/conflicting args
            # `update_index` expects `using` to be a list (it appends multiple
            # values). If we pass a string, Django will iterate it as an
            # iterable of characters which causes haystack to try connection
            # aliases like 'd', 'e', etc. Pass a single-item list instead.
            using_arg = [using] if isinstance(using, str) else using
            if verbosity is not None:
                management.call_command(
                    "update_index", using=using_arg, verbosity=verbosity
                )
            else:
                management.call_command("update_index", using=using_arg)
            self.stdout.write(self.style.SUCCESS("Haystack update_index completed successfully."))
        except Exception as e:
            logger.exception("update_index failed")
            self.stderr.write("Haystack update_index failed: %s" % e)
            raise
