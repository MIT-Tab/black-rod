from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.account.utils import user_email, user_field, user_username

from core.models import User


class APDAOnlineAdapter(DefaultSocialAccountAdapter):
    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        user_field(user, 'can_view_private_videos', data.get('can_view_private_videos'))
        return user
