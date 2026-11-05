# pylint: disable=protected-access
"""Keep the search index consistent with committed data, including agent undo."""

from functools import partial

from django.db import transaction
from haystack.signals import RealtimeSignalProcessor


class CommitSignalProcessor(RealtimeSignalProcessor):
    """A rolled-back dry run must never edit the external search index."""

    def synchronize(self, sender, pk, using="default"):
        instance = sender._default_manager.using(using).filter(pk=pk).first()
        if instance is None:
            # Haystack accepts an instance with just the original primary key.
            super().handle_delete(sender, sender(pk=pk))
        else:
            super().handle_save(sender, instance)

    def handle_save(self, sender, instance, **kwargs):
        backends = self.connection_router.for_write(instance=instance)
        if not any(
            sender in self.connections[backend].get_unified_index().get_indexed_models()
            for backend in backends
        ):
            return
        transaction.on_commit(
            partial(
                self.synchronize, sender, instance.pk, kwargs.get("using", "default")
            ),
            using=kwargs.get("using", "default"),
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
