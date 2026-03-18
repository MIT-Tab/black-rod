from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.models import Permission
from django.test import TestCase

from core.access import can_download_debater_tab_cards
from core.models import Debater, DebaterAliasGroup, School, User


class DebaterTabCardAccessTest(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="Access Test School")
        self.owner = User.objects.create_user(username="owner", password="testpass")
        self.other_user = User.objects.create_user(username="other", password="testpass")
        self.debater = Debater.objects.create(
            first_name="Casey",
            last_name="Owner",
            school=self.school,
            user=self.owner,
        )

    def test_requires_linked_debater_without_debug_permission(self):
        self.assertTrue(can_download_debater_tab_cards(self.owner, self.debater))
        self.assertFalse(can_download_debater_tab_cards(self.other_user, self.debater))

    def test_allows_owner_of_linked_alias_group(self):
        alias_group = DebaterAliasGroup.objects.create(label="Casey Owner")
        self.debater.alias_group = alias_group
        self.debater.user = None
        self.debater.save()

        linked_debater = Debater.objects.create(
            first_name="Casey",
            last_name="Owner",
            school=self.school,
            alias_group=alias_group,
            user=self.owner,
        )

        self.assertTrue(can_download_debater_tab_cards(self.owner, self.debater))
        self.assertTrue(can_download_debater_tab_cards(self.owner, linked_debater))
        self.assertFalse(can_download_debater_tab_cards(self.other_user, self.debater))

    def test_allows_user_with_debug_csv_permission(self):
        permission = Permission.objects.get(codename="can_view_debug_tab_cards")
        self.other_user.user_permissions.add(permission)

        self.assertTrue(can_download_debater_tab_cards(self.other_user, self.debater))

    def test_still_blocks_anonymous_users(self):
        self.assertFalse(can_download_debater_tab_cards(AnonymousUser(), self.debater))
