from django.dispatch import receiver
from allauth.account.signals import user_logged_in
from allauth.socialaccount.models import SocialAccount, SocialToken


APDA_USERINFO_URL = "https://apda.online/oauth/me"

@receiver(user_logged_in)
def sync_apda_permissions_on_login(request, user, **kwargs):
    print("Yipee we're here!")
    try:
        sa = SocialAccount.objects.get(user=user, provider="apdaonline")
    except SocialAccount.DoesNotExist:
        return  

    
    roles = sa.extra_data.get("user_roles", []) or []
    can_view = "private_side_viewer" in roles
    if user.can_view_private_videos != can_view:
        user.can_view_private_videos = can_view
        user.save(update_fields=["can_view_private_videos"])

    

