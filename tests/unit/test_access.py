from django.contrib.auth.models import AnonymousUser
from django.test import TestCase, override_settings

from core.access import can_download_debater_tab_cards
from core.models import Debater, School, User


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

    @override_settings(ENV="production")
    def test_requires_linked_debater_outside_development(self):
        self.assertTrue(can_download_debater_tab_cards(self.owner, self.debater))
        self.assertFalse(can_download_debater_tab_cards(self.other_user, self.debater))

    @override_settings(ENV="development")
    def test_allows_any_authenticated_user_in_development(self):
        self.assertTrue(can_download_debater_tab_cards(self.other_user, self.debater))

    @override_settings(ENV="development")
    def test_still_blocks_anonymous_users_in_development(self):
        self.assertFalse(can_download_debater_tab_cards(AnonymousUser(), self.debater))
