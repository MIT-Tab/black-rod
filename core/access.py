from django.conf import settings

from core.models import Debater


EXCLUSIVE_PRE_ACCESS_PERMISSION = "core.exclusive_pre_access"


def has_exclusive_pre_access(user):
    return bool(
        getattr(user, "is_authenticated", False)
        and user.has_perm(EXCLUSIVE_PRE_ACCESS_PERMISSION)
    )


def can_download_debater_tab_cards(user, debater):
    if not getattr(user, "is_authenticated", False):
        return False
    if not isinstance(debater, Debater):
        return False
    if getattr(settings, "ENV", "") == "development":
        return True
    return bool(debater.user_id and debater.user_id == user.id)
