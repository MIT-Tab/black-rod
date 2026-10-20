from unittest.mock import patch
from datetime import date

import pytest
from django.core.cache import cache
from django.test import Client, override_settings

from core.live_results import build_live_standings
from core.models import School, Tournament


def _payload(name="MIT Fall", sanctioned=True, place=3):
    return {
        "schema_version": 1,
        "source_url": "https://mit.nu-tab.com",
        "tournament": {
            "name": name,
            "date": "2026-10-19",
            "season": "2026",
            "host_school": {"apda_id": 1, "name": "MIT"},
            "tournament_type": 0,
            "sanctioned": sanctioned,
            "num_teams": 32,
            "num_novice_debaters": 8,
        },
        "team_results": [{
            "place": place,
            "division": "varsity",
            "team": {
                "name": "MIT AB",
                "debaters": [
                    {"apda_id": 101, "name": "Ada A", "school": {"apda_id": 1, "name": "MIT"}},
                    {"apda_id": 102, "name": "Bea B", "school": {"apda_id": 1, "name": "MIT"}},
                ],
            },
        }],
        "speaker_results": [],
    }


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_live_standings_are_ephemeral_and_cached_for_the_page():
    cache.clear()
    with patch("core.live_results.fetch_live_sources", return_value=[_payload()]) as fetch:
        first = build_live_standings()
        second = build_live_standings()

    assert first == second
    fetch.assert_called_once()
    assert first["active_sources"] == ["MIT Fall"]
    assert first["boards"]["toty"][0]["has_live"] is True
    assert first["boards"]["toty"][0]["markers"][0]["placement"] == 3


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_unsanctioned_tournament_does_not_affect_standings():
    cache.clear()
    with patch(
        "core.live_results.fetch_live_sources",
        return_value=[_payload(sanctioned=False)],
    ):
        result = build_live_standings()

    assert result["active_sources"] == []
    assert result["boards"]["toty"] == []


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_live_page_labels_provisional_results():
    cache.clear()
    with patch("core.live_results.fetch_live_sources", return_value=[_payload()]):
        response = Client().get("/live")

    assert response.status_code == 200
    assert b"have not been reviewed by a human" in response.content
    assert b"MIT Fall" in response.content
    assert b"<th scope=\"col\">School</th>" in response.content
    assert b"<th scope=\"col\">Debaters</th>" in response.content
    assert b"<mark class=\"bg-warning\">" in response.content
    assert b"Results counting toward total" not in response.content


@pytest.mark.django_db
@override_settings(CURRENT_SEASON="2026")
def test_already_imported_tournament_is_not_counted_twice():
    cache.clear()
    host = School.objects.create(id=1, name="MIT", short_name="MIT")
    Tournament.objects.create(
        host=host,
        season="2026",
        date=date(2026, 10, 19),
        qual_type=Tournament.POINTS,
        num_teams=32,
        num_novice_teams=0,
        num_debaters=64,
        num_novice_debaters=8,
    )
    with patch("core.live_results.fetch_live_sources", return_value=[_payload()]):
        result = build_live_standings()

    assert result["active_sources"] == []
    assert result["boards"]["toty"] == []
