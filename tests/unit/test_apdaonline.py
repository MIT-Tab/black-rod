from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialAccount, SocialApp
from allauth.socialaccount.providers.oauth2.client import OAuth2Client
from django.contrib.auth import get_user_model
from django.urls import reverse

from apdaonline.adapter import APDAOnlineAdapter
from apdaonline.provider import APDAOnlineProvider
from apdaonline.signals import sync_apda_permissions_on_login


pytestmark = pytest.mark.django_db


def test_adapter_populate_user_propagates_private_video_flag(monkeypatch):
    adapter = APDAOnlineAdapter()
    sentinel_user = SimpleNamespace(can_view_private_videos=False)

    def fake_populate(self, request, sociallogin, data):  # pylint: disable=unused-argument
        return sentinel_user

    monkeypatch.setattr(DefaultSocialAccountAdapter, "populate_user", fake_populate)

    result = adapter.populate_user(None, None, {"can_view_private_videos": True})

    assert result.can_view_private_videos is True


def test_provider_extracts_uid_and_fields():
    provider = APDAOnlineProvider(request=None, app=SocialApp())
    payload = {
        "ID": 42,
        "user_nicename": "testuser",
        "user_email": "user@example.com",
        "display_name": "Test User",
        "user_roles": ["private_side_viewer", "member"],
    }

    assert provider.extract_uid(payload) == "42"

    fields = provider.extract_common_fields(payload)

    assert fields == {
        "username": "testuser",
        "email": "user@example.com",
        "can_view_private_videos": True,
        "name": "Test User",
    }

    payload["user_roles"] = []
    assert provider.extract_common_fields(payload)["can_view_private_videos"] is False


def test_apdaonline_oauth_login(client, monkeypatch):
    user = get_user_model().objects.create_user(username="linked-user")
    SocialAccount.objects.create(user=user, provider="apdaonline", uid="42")
    monkeypatch.setattr(
        OAuth2Client, "get_access_token",
        lambda *args, **kwargs: {"access_token": "test-token"},
    )
    monkeypatch.setattr(
        "apdaonline.views.requests.get",
        lambda *args, **kwargs: SimpleNamespace(json=lambda: {
            "ID": 42,
            "user_nicename": "linked-user",
            "user_email": "user@example.com",
            "display_name": "Linked User",
            "user_roles": ["private_side_viewer"],
        }),
    )

    response = client.post(reverse("apdaonline_login"))

    assert response.status_code == 302
    redirect = urlsplit(response["Location"])
    assert (redirect.scheme, redirect.netloc, redirect.path) == (
        "https", "apda.online", "/oauth/authorize",
    )
    params = parse_qs(redirect.query)
    assert params["redirect_uri"] == [f"http://testserver{reverse('apdaonline_callback')}"]
    callback = client.get(
        reverse("apdaonline_callback"), {"code": "test-code", "state": params["state"][0]},
    )

    assert callback.status_code == 302
    assert client.session["_auth_user_id"] == str(user.pk)
    user.refresh_from_db()
    assert user.can_view_private_videos is True


def test_sync_apda_permissions_grants_and_revokes_access():
    user = get_user_model().objects.create_user(
        username="grantee",
        password="test-pass",
    )
    user.can_view_private_videos = False
    user.save()

    SocialAccount.objects.create(
        user=user,
        provider="apdaonline",
        extra_data={"user_roles": ["private_side_viewer"]},
    )

    sync_apda_permissions_on_login(None, user)
    user.refresh_from_db()
    assert user.can_view_private_videos is True

    account = SocialAccount.objects.get(user=user, provider="apdaonline")
    account.extra_data = {"user_roles": []}
    account.save(update_fields=["extra_data"])

    sync_apda_permissions_on_login(None, user)
    user.refresh_from_db()
    assert user.can_view_private_videos is False


def test_sync_apda_permissions_ignores_missing_account():
    user = get_user_model().objects.create_user(
        username="nomatch",
        password="test-pass",
    )
    user.can_view_private_videos = True
    user.save()

    sync_apda_permissions_on_login(None, user)
    user.refresh_from_db()
    assert user.can_view_private_videos is True
