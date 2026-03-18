from core.models import Debater
from core.utils.debater_aliases import load_linked_debater_ids


EXCLUSIVE_PRE_ACCESS_PERMISSION = "core.exclusive_pre_access"
VIEW_DEBUG_TAB_CARDS_PERMISSION = "core.can_view_debug_tab_cards"


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
    if user.has_perm(VIEW_DEBUG_TAB_CARDS_PERMISSION):
        return True

    linked_debater_ids = load_linked_debater_ids([debater.id])
    if not linked_debater_ids:
        return False

    return Debater.all_objects.filter(id__in=linked_debater_ids, user_id=user.id).exists()
