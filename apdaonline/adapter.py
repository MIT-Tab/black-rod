from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


class APDAOnlineAdapter(DefaultSocialAccountAdapter):
    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        user.can_view_private_videos = data.get('can_view_private_videos', False)
        return user
