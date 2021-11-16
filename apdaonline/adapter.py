from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.account.utils import user_email, user_field, user_username

from core.models import User


class APDAOnlineAdapter(DefaultSocialAccountAdapter):
    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)

        u = User.objects.filter(username=user.username).first()

        if u:
            user = u

        user.save()
        user.user_permissions.set(data.get('user_permissions'))
        
        return user
