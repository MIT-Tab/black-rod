from allauth.socialaccount import providers
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.providers.base import ProviderAccount
from allauth.socialaccount.providers.oauth2.provider import OAuth2Provider


class APDAOnlineAccount(ProviderAccount):
    pass


class APDAOnlineProvider(OAuth2Provider):
    id = "apdaonline"
    name = "APDAOnline"
    account_class = APDAOnlineAccount

    def extract_uid(self, data):
        return str(data['ID'])

    def extract_common_fields(self, data):
        can_view_private_videos = False
        if 'private_side_viewer' in data['user_roles']:
            can_view_private_videos = True

        return {
            "username": data['user_nicename'],
            "email": data['user_email'],
            "can_view_private_videos": can_view_private_videos,
            "name": data['display_name']
        }

    def sociallogin_from_response(self, request, response):
        sociallogin = super().sociallogin_from_response(request, response)

        print (get_adapter(request))

        return sociallogin

providers.registry.register(APDAOnlineProvider)
