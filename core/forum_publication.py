"""Attach permanent Black Rod tab cards to the tournament's automated forum post."""

import logging
import re
from functools import partial

import requests
from django.conf import settings
from django.db import transaction

from core.models.tournament import Tournament
from core.utils.rounds import visible_canonical_rounds

LOGGER = logging.getLogger(__name__)


def configured():
    return bool(settings.APDA_FORUM_URL and settings.APDA_BLACK_ROD_FORUM_TOKEN)


def valid_publication_id(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) is not None


class ForumPublicationConflict(Exception):
    """A tournament was matched to a different forum posting ID."""


def schedule_tab_cards(tournament, publication_id):
    if not (publication_id or tournament.forum_publication_id):
        return
    saved = Tournament.objects.select_for_update().get(pk=tournament.pk)
    if publication_id and saved.forum_publication_id != publication_id:
        if saved.forum_publication_id or Tournament.objects.filter(forum_publication_id=publication_id).exists():
            raise ForumPublicationConflict("This forum posting ID conflicts with a tournament already linked to the forum.")
        Tournament.objects.filter(pk=saved.pk).update(forum_publication_id=publication_id)
    if configured() and not saved.forum_tab_cards_synced:
        transaction.on_commit(partial(sync_tab_cards, saved.pk), robust=True)


def sync_tab_cards(tournament_id):
    if not configured():
        return
    tournament = Tournament.objects.filter(
        pk=tournament_id, forum_tab_cards_synced=False, forum_publication_id__isnull=False,
    ).first()
    if not tournament or not visible_canonical_rounds(tournament.rounds.all()).exists():
        return
    endpoint = (
        f"{settings.APDA_FORUM_URL}/wp-json/apda-forum/v1/tournaments/"
        f"{tournament.forum_publication_id}/tab-cards"
    )
    tab_cards_url = f"{settings.APDA_RESULTS_ORIGIN}{tournament.get_absolute_url()}#tab-cards"
    try:
        response = requests.post(
            endpoint,
            json={"url": tab_cards_url},
            headers={"X-APDA-Results-Token": settings.APDA_BLACK_ROD_FORUM_TOKEN},
            timeout=(3, 5),
            allow_redirects=False,
        )
        if response.status_code != 200:
            LOGGER.warning("Forum tab card delivery for tournament %s returned HTTP %s",
                           tournament_id, response.status_code)
            return
    except requests.RequestException:
        LOGGER.warning("Forum tab card delivery failed for tournament %s", tournament_id)
        return
    Tournament.objects.filter(pk=tournament_id).update(forum_tab_cards_synced=True)
