from types import SimpleNamespace

import pytest
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialAccount
from allauth.socialaccount.providers.oauth2.provider import OAuth2Provider
from django.contrib.auth import get_user_model

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
    provider = APDAOnlineProvider(request=None)
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


def test_provider_sociallogin_uses_oauth_base(monkeypatch):
    provider = APDAOnlineProvider(request=None)
    sentinel = object()

    def fake_sociallogin(self, request, response):  # pylint: disable=unused-argument
        return sentinel

    monkeypatch.setattr(OAuth2Provider, "sociallogin_from_response", fake_sociallogin)

    result = provider.sociallogin_from_response(None, {"ID": 1})

    assert result is sentinel


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
