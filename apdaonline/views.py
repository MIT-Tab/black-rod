import requests
from allauth.socialaccount.providers.oauth2.views import (
    OAuth2Adapter,
    OAuth2CallbackView,
    OAuth2LoginView,
)

from .provider import APDAOnlineProvider


class APDAOnlineOAuth2Adapter(OAuth2Adapter):
    provider_id = APDAOnlineProvider.id

    access_token_url = "https://apda.online/oauth/token"
    authorize_url = "https://apda.online/oauth/authorize"
    profile_url = "https://apda.online/oauth/me"

    def complete_login(self, request, app, access_token, **kwargs):
        headers = {"Authorization": f"Bearer {access_token.token}"}
        resp = requests.get(self.profile_url, headers=headers, timeout=30)
        extra_data = resp.json()
        return self.get_provider().sociallogin_from_response(request, extra_data)


oauth2_login = OAuth2LoginView.adapter_view(APDAOnlineOAuth2Adapter)
oauth2_callback = OAuth2CallbackView.adapter_view(APDAOnlineOAuth2Adapter)
