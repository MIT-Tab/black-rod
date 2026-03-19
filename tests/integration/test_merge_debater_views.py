import json

import pytest
from django.conf import settings
from django.urls import reverse

from core.models import Debater, MergeDebaterRequest, School, SchoolAdmin, User


@pytest.mark.django_db
def test_merge_request_view_requires_login(client):
    url = reverse("core:merge_debater_request_create")
    response = client.get(url)
    assert response.status_code == 302
    assert settings.LOGIN_URL in response.url


@pytest.mark.django_db
def test_school_admin_can_submit_merge_request(client):
    user = User.objects.create_user(username="admin", password="pass")
    school = School.objects.create(name="Request School")
    SchoolAdmin.objects.create(user=user, school=school)

    debater_a = Debater.objects.create(
        first_name="Req",
        last_name="One",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    debater_b = Debater.objects.create(
        first_name="Req",
        last_name="Two",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )

    client.force_login(user)
    response = client.post(
        reverse("core:merge_debater_request_create"),
        data={
            "school_one": school.pk,
            "debater_one": debater_a.pk,
            "school_two": school.pk,
            "debater_two": debater_b.pk,
            "keep_debater": "debater_one",
        },
        follow=True,
    )

    assert response.status_code == 200
    assert MergeDebaterRequest.objects.count() == 1
    req = MergeDebaterRequest.objects.first()
    assert req.primary_debater == debater_a
    assert req.secondary_debater == debater_b
    assert req.status == MergeDebaterRequest.STATUS_PENDING
    assert req.primary_name == debater_a.name
    assert req.primary_school_name == debater_a.school.name
    assert req.secondary_name == debater_b.name
    assert req.secondary_school_name == debater_b.school.name


@pytest.mark.django_db
def test_merge_request_page_does_not_preload_all_debaters(client):
    user = User.objects.create_user(username="admin", password="pass")
    school = School.objects.create(name="Request School")
    SchoolAdmin.objects.create(user=user, school=school)

    debater = Debater.objects.create(
        first_name="Unique",
        last_name="VisibleOnSearchOnly",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )

    client.force_login(user)
    response = client.get(reverse("core:merge_debater_request_create"))

    assert response.status_code == 200
    assert debater.name not in response.content.decode()


@pytest.mark.django_db
def test_merge_debater_autocomplete_excludes_synthetic_and_old_debaters(client):
    user = User.objects.create_user(username="admin", password="pass")
    school = School.objects.create(name="Request School")
    SchoolAdmin.objects.create(user=user, school=school)

    active = Debater.objects.create(
        first_name="Merge",
        last_name="Candidate",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    Debater.all_objects.create(
        first_name="Merge",
        last_name="Synthetic",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
        temporary=True,
        synthetic=True,
    )
    Debater.objects.create(
        first_name="Merge",
        last_name="Archived",
        school=school,
        first_season=str(int(settings.CURRENT_SEASON) - 2),
        latest_season=str(int(settings.CURRENT_SEASON) - 2),
    )

    client.force_login(user)
    response = client.get(
        reverse("core:merge_debater_autocomplete"),
        {
            "q": "Merge",
            "forward": json.dumps({"school": school.pk}),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    labels = [item["text"] for item in payload["results"]]

    assert any(str(active.id) in label for label in labels)
    assert not any("Synthetic" in label for label in labels)
    assert not any("Archived" in label for label in labels)


@pytest.mark.django_db
def test_non_superuser_cannot_access_review(client):
    user = User.objects.create_user(username="regular", password="pass")
    client.force_login(user)
    response = client.get(reverse("core:merge_debater_request_review"))
    assert response.status_code == 403


@pytest.mark.django_db
def test_superuser_can_approve_merge_request(client):
    school = School.objects.create(name="Review School")
    primary = Debater.objects.create(
        first_name="Keep",
        last_name="Me",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    secondary = Debater.objects.create(
        first_name="Merge",
        last_name="Me",
        school=school,
        first_season=settings.CURRENT_SEASON,
        latest_season=settings.CURRENT_SEASON,
    )
    request_obj = MergeDebaterRequest.objects.create(
        requested_by=User.objects.create_user(username="requestor"),
        primary_debater=primary,
        secondary_debater=secondary,
    )

    superuser = User.objects.create_superuser(
        username="super", email="super@example.com", password="pass"
    )
    client.force_login(superuser)

    response = client.post(
        reverse("core:merge_debater_request_review"),
        data={"request_id": request_obj.id, "action": "approve"},
        follow=True,
    )

    assert response.status_code == 200
    request_obj.refresh_from_db()
    assert request_obj.status == MergeDebaterRequest.STATUS_APPROVED
    assert request_obj.processed_by == superuser
    assert not Debater.objects.filter(pk=secondary.pk).exists()
    assert request_obj.secondary_debater is None
    assert request_obj.secondary_name == "Merge Me"
