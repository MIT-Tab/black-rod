from core.models.video import Video


def has_perm(user, video):
    if (
        user.is_superuser
        or user.has_perm("core.view_video")
        or video.permissions == Video.ALL
    ):
        return True

    if not user.is_authenticated:
        return False

    if video.permissions == Video.ACCOUNTS_ONLY and user.can_view_private_videos:
        return True

    if video.permissions == Video.DEBATERS_IN_ROUND:
        return False

    return False
