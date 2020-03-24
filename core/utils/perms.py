from core.models.video import Video


def has_perm(user, video):
    if user.is_superuser:
        return True

    if user.has_perm('core.view_video'):
        return True

    if video.permissions == Video.ALL:
        return True

    if video.permissions == Video.ACCOUNTS_ONLY \
       and user.is_authenticated:
        return False

    if video.permissions == Video.DEBATERS_IN_ROUND:
        return False

    return False
