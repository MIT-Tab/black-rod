from datetime import date
from unittest.mock import Mock

import pytest
import requests
from django.db import transaction

from core import forum_publication
from core.models import Round, School, Team, Tournament


@pytest.fixture
def tournament(db):
    school = School.objects.create(name="Example School")
    return Tournament.objects.create(host=school, date=date(2026, 10, 15), season="2026")


@pytest.mark.django_db
def test_tab_card_delivery_waits_for_saved_rounds_and_can_retry(
    settings, tournament, monkeypatch, django_capture_on_commit_callbacks,
):
    settings.APDA_FORUM_URL = "https://forum.example.test"
    settings.APDA_BLACK_ROD_FORUM_TOKEN = "test-token"
    settings.APDA_RESULTS_ORIGIN = "https://results.apda.online"
    publication_id = "a" * 32
    post = Mock()
    monkeypatch.setattr(forum_publication.requests, "post", post)

    with django_capture_on_commit_callbacks(execute=True):
        with transaction.atomic():
            forum_publication.schedule_tab_cards(tournament, publication_id)
            post.assert_not_called()

    tournament.refresh_from_db()
    assert tournament.forum_publication_id == publication_id
    assert not tournament.forum_tab_cards_synced
    post.assert_not_called()

    gov = Team.objects.create(name="Gov")
    opp = Team.objects.create(name="Opp")
    Round.objects.create(tournament=tournament, gov=gov, opp=opp)
    post.side_effect = [requests.Timeout(), Mock(status_code=200)]
    forum_publication.sync_tab_cards(tournament.pk)
    tournament.refresh_from_db()
    assert not tournament.forum_tab_cards_synced

    forum_publication.sync_tab_cards(tournament.pk)
    post.assert_called_with(
        f"https://forum.example.test/wp-json/apda-forum/v1/tournaments/{publication_id}/tab-cards",
        json={"url": f"https://results.apda.online{tournament.get_absolute_url()}#tab-cards"},
        headers={"X-APDA-Results-Token": "test-token"},
        timeout=(3, 5),
        allow_redirects=False,
    )
    forum_publication.sync_tab_cards(tournament.pk)
    tournament.refresh_from_db()
    assert tournament.forum_tab_cards_synced
    assert post.call_count == 2


@pytest.mark.django_db
def test_a_rolled_back_import_does_not_queue_a_forum_update(
    settings, tournament, monkeypatch, django_capture_on_commit_callbacks,
):
    settings.APDA_FORUM_URL = "https://forum.example.test"
    settings.APDA_BLACK_ROD_FORUM_TOKEN = "test-token"
    sync = Mock()
    monkeypatch.setattr(forum_publication, "sync_tab_cards", sync)
    with django_capture_on_commit_callbacks(execute=True):
        with transaction.atomic():
            forum_publication.schedule_tab_cards(tournament, "b" * 32)
            transaction.set_rollback(True)
    sync.assert_not_called()
    tournament.refresh_from_db()
    assert tournament.forum_publication_id is None
