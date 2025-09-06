from allauth.socialaccount.providers.oauth2.urls import default_urlpatterns

from .provider import APDAOnlineProvider

urlpatterns = default_urlpatterns(APDAOnlineProvider)
