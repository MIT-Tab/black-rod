from datetime import date
import json

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from core.models import Debater, School, Team, TeamResult, Tournament


pytestmark = pytest.mark.django_db


def _make_tournament(with_results=True):
    host = School.objects.create(name="Upload Host", short_name="UH")
    tournament = Tournament.objects.create(
        name="Upload Open",
        manual_name="Upload Open",
        host=host,
        date=date(2024, 2, 1),
        season="2024",
    )
    if with_results:
        team = Team.objects.create(name="Upload Result Team", short_name="URT")
        team.debaters.set(
            [
                Debater.objects.create(first_name="Res", last_name="One", school=host),
                Debater.objects.create(first_name="Res", last_name="Two", school=host),
            ]
        )
        TeamResult.objects.create(tournament=tournament, team=team, place=1)
    return tournament


def _bundle_file():
    payload = {
        "schema_version": 1,
        "source": "mit_tab_black_rod_bundle",
        "exported_at": "2026-03-19T00:00:00+00:00",
        "tournament_name": "Mit Tab Open",
        "schools": [{"id": 1, "apda_id": None, "name": "Fallback School"}],
        "debaters": [
            {"id": 101, "apda_id": None, "name": "Gov One", "novice_status": "varsity", "school_id": 1},
            {"id": 102, "apda_id": None, "name": "Gov Two", "novice_status": "varsity", "school_id": 1},
            {"id": 201, "apda_id": None, "name": "Opp One", "novice_status": "varsity", "school_id": 1},
            {"id": 202, "apda_id": None, "name": "Opp Two", "novice_status": "varsity", "school_id": 1},
        ],
        "rounds": [
            {
                "import_key": "prelim:1",
                "round_number": 1,
                "label": "Round 1",
                "stage": "prelim",
                "division": None,
                "elim_size": None,
                "victor": 1,
                "gov": {"debater_ids": [101, 102], "source_names": ["Gov One", "Gov Two"]},
                "opp": {"debater_ids": [201, 202], "source_names": ["Opp One", "Opp Two"]},
                "judges": [{"original_name": "Judge Prime", "is_chair": True}],
            }
        ],
    }
    return SimpleUploadedFile(
        "bundle.json",
        json.dumps(payload).encode("utf-8"),
        content_type="application/json",
    )


def test_superuser_can_view_upload_page_and_button(client):
    tournament = _make_tournament()
    user = get_user_model().objects.create_superuser(
        username="admin",
        email="admin@example.com",
        password="pass123",
    )
    client.login(username="admin", password="pass123")

    detail_response = client.get(reverse("core:tournament_detail", kwargs={"pk": tournament.pk}))
    upload_response = client.get(
        reverse("core:tournament_mittab_bundle_upload") + f"?tournament={tournament.id}"
    )

    assert detail_response.status_code == 200
    assert "Upload Mit-Tab Bundle" in detail_response.content.decode("utf-8")
    assert upload_response.status_code == 200
    assert "Upload Mit-Tab Bundle: Upload Open" in upload_response.content.decode("utf-8")


def test_non_superuser_cannot_access_upload_page(client):
    tournament = _make_tournament()
    user = get_user_model().objects.create_user(
        username="editor",
        email="editor@example.com",
        password="pass123",
    )
    user.user_permissions.add(Permission.objects.get(codename="change_tournament"))
    client.login(username="editor", password="pass123")

    detail_response = client.get(reverse("core:tournament_detail", kwargs={"pk": tournament.pk}))
    upload_response = client.get(
        reverse("core:tournament_mittab_bundle_upload") + f"?tournament={tournament.id}"
    )

    assert detail_response.status_code == 200
    assert "Upload Mit-Tab Bundle" not in detail_response.content.decode("utf-8")
    assert upload_response.status_code == 403


def test_upload_page_warns_when_results_missing(client):
    tournament = _make_tournament(with_results=False)
    user = get_user_model().objects.create_superuser(
        username="admin2",
        email="admin2@example.com",
        password="pass123",
    )
    client.login(username="admin2", password="pass123")

    response = client.post(
        reverse("core:tournament_mittab_bundle_upload"),
        data={"tournament": tournament.id, "bundle_file": _bundle_file()},
    )

    assert response.status_code == 200
    assert "Results must be imported for this tournament before uploading a Mit-Tab bundle." in response.content.decode(
        "utf-8"
    )
