# pylint: disable=protected-access
"""Keep the search index consistent with committed data, including agent undo."""

from contextlib import suppress
from functools import partial
from hashlib import sha256
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.db import connections, transaction
from haystack.signals import RealtimeSignalProcessor


LOGGER = logging.getLogger(__name__)


def search_marker(using):
    connection = connections[using]
    database = connection.settings_dict
    if connection.vendor == "sqlite":
        name = str(database["NAME"])
        if name.startswith("file:"):
            uri = urlsplit(name)
            if "memory" in parse_qs(uri.query).get("mode", []):
                return None
            name = unquote(uri.path)
        if name == ":memory:":
            return None
        identity = f"sqlite\0{Path(name).resolve()}"
    elif connection.vendor == "postgresql":
        host = (database["HOST"] or "localhost").lower()
        port = int(database["PORT"] or 5432)
        identity = f"postgres\0{host}\0{port}\0{database['NAME']}"
    else:
        return None
    if override := os.environ.get("BLACK_ROD_SEARCH_GENERATION_FILE"):
        return Path(override)
    root = Path(os.environ.get("BLACK_ROD_RELEASE_ROOT", settings.PROJECT_ROOT))
    return root / "tmp" / f"search-{sha256(identity.encode()).hexdigest()}.version"


def invalidate_rust_search(using="default"):
    generation = uuid4().bytes
    backend = settings.CACHES["default"]["BACKEND"]
    if backend.startswith("django.core.cache.backends.memcached."):
        cache.set("black-rod:search-index:generation:v2", generation, timeout=None)
        return
    marker = search_marker(using)
    if marker is None:
        return
    pending = None
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=marker.parent, delete=False) as output:
            pending = Path(output.name)
            output.write(generation)
        pending.replace(marker)
    except OSError:
        LOGGER.exception("Could not update the local search index generation")
    finally:
        if pending is not None:
            with suppress(OSError):
                pending.unlink(missing_ok=True)


class CommitSignalProcessor(RealtimeSignalProcessor):
    """A rolled-back dry run must never edit the external search index."""

    def synchronize(self, sender, pk, using="default"):
        try:
            instance = sender._default_manager.using(using).filter(pk=pk).first()
            if instance is None:
                # Haystack accepts an instance with just the original primary key.
                super().handle_delete(sender, sender(pk=pk))
            else:
                super().handle_save(sender, instance)
        finally:
            invalidate_rust_search(using)

    def handle_save(self, sender, instance, **kwargs):
        using = kwargs.get("using", "default")
        if sender._meta.label_lower == "core.school":
            transaction.on_commit(partial(invalidate_rust_search, using), using=using, robust=True)
        backends = self.connection_router.for_write(instance=instance)
        if not any(
            sender in self.connections[backend].get_unified_index().get_indexed_models()
            for backend in backends
        ):
            return
        transaction.on_commit(
            partial(
                self.synchronize, sender, instance.pk, using
            ),
            using=using,
            robust=True,
        )

    def handle_delete(self, sender, instance, **kwargs):
        self.handle_save(sender, instance, **kwargs)


def synchronize_debaters(ids):
    """Raw journal restoration bypasses save/delete signals intentionally."""
    from django.apps import apps  # pylint: disable=import-outside-toplevel
    from core.models import Debater  # pylint: disable=import-outside-toplevel

    signal_processor = apps.get_app_config("haystack").signal_processor
    if isinstance(signal_processor, CommitSignalProcessor):
        for pk in ids:
            signal_processor.synchronize(Debater, pk)
