from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from core.models.tournament import Tournament


RECENT_RESULTS_WIDGET_CACHE_KEY = "recent_results_widget_html"


@receiver(post_save, sender=Tournament)
def clear_recent_results_widget_cache_on_tournament_save(sender, **kwargs):
    cache.delete(RECENT_RESULTS_WIDGET_CACHE_KEY)


@receiver(post_delete, sender=Tournament)
def clear_recent_results_widget_cache_on_tournament_delete(sender, **kwargs):
    cache.delete(RECENT_RESULTS_WIDGET_CACHE_KEY)
