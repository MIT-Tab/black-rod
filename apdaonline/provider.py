from allauth.socialaccount.providers.base import ProviderAccount
from allauth.socialaccount.providers.oauth2.provider import OAuth2Provider

from .views import APDAOnlineOAuth2Adapter


class APDAOnlineAccount(ProviderAccount):
    pass


class APDAOnlineProvider(OAuth2Provider):  # pylint: disable=abstract-method
    # This provider uses browser redirects and does not accept posted tokens.
    id = APDAOnlineOAuth2Adapter.provider_id
    name = "APDAOnline"
    account_class = APDAOnlineAccount
    oauth2_adapter_class = APDAOnlineOAuth2Adapter

    def extract_uid(self, data):
        return str(data["ID"])

    def extract_common_fields(self, data):
        can_view_private_videos = False
        if "private_side_viewer" in data["user_roles"]:
            can_view_private_videos = True

        return {
            "username": data["user_nicename"],
            "email": data["user_email"],
            "can_view_private_videos": can_view_private_videos,
            "name": data["display_name"],
        }


provider_classes = [APDAOnlineProvider]
