from datetime import date
from types import SimpleNamespace

from django.test import TestCase

from core.models import Debater, School, Team, Tournament, Video
from core.utils.perms import has_perm
from core.utils.team import get_or_create_team_for_debaters


class TeamUtilityTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="Team School", included_in_oty=True)
        self.debater_one = Debater.objects.create(
            first_name="Dana", last_name="Smith", school=self.school
        )
        self.debater_two = Debater.objects.create(
            first_name="Rory", last_name="Nguyen", school=self.school
        )

    def test_get_or_create_team_creates_and_names_team(self):
        team = get_or_create_team_for_debaters(self.debater_one, self.debater_two)

        self.assertEqual(team.debaters.count(), 2)
        self.assertTrue(team.name.startswith(self.school.name))
        self.assertIn(self.debater_one.last_name[0], team.name)
        self.assertIn(self.debater_two.last_name[0], team.name)

    def test_get_or_create_team_returns_existing_team(self):
        existing = Team.objects.create()
        existing.debaters.add(self.debater_one, self.debater_two)
        existing.update_name()
        existing.save()

        returned = get_or_create_team_for_debaters(self.debater_one, self.debater_two)

        self.assertEqual(returned.id, existing.id)

    def test_update_name_handles_unaffiliated_debaters(self):
        unaffiliated = Debater.objects.create(
            first_name="Unaffiliated",
            last_name="Partner",
            school=None,
        )
        team = Team.objects.create()
        team.debaters.add(self.debater_one, unaffiliated)

        team.update_name()

        self.assertEqual(team.name, "Team School / Unaffiliated SP")


class VideoPermissionTests(TestCase):
    def setUp(self):
        school = School.objects.create(name="Video School", included_in_oty=True)
        self.debaters = [
            Debater.objects.create(first_name="A", last_name="One", school=school),
            Debater.objects.create(first_name="B", last_name="Two", school=school),
            Debater.objects.create(first_name="C", last_name="Three", school=school),
            Debater.objects.create(first_name="D", last_name="Four", school=school),
        ]
        tournament = Tournament.objects.create(
            name="Video Tournament",
            host=school,
            season="2024",
            date=date(2024, 1, 1),
            num_teams=16,
        )
        self.video = Video.objects.create(
            pm=self.debaters[0],
            lo=self.debaters[1],
            mg=self.debaters[2],
            mo=self.debaters[3],
            tournament=tournament,
            link="https://example.com/video",
        )

    def test_superuser_can_view_any_video(self):
        user = SimpleNamespace(
            is_superuser=True,
            is_authenticated=True,
            can_view_private_videos=False,
            has_perm=lambda perm: False,
        )

        self.video.permissions = Video.DEBATERS_IN_ROUND
        self.assertTrue(has_perm(user, self.video))

    def test_authenticated_user_with_view_permission_is_allowed(self):
        user = SimpleNamespace(
            is_superuser=False,
            is_authenticated=True,
            can_view_private_videos=False,
            has_perm=lambda perm: perm == "core.view_video",
        )

        self.video.permissions = Video.DEBATERS_IN_ROUND
        self.assertTrue(has_perm(user, self.video))

    def test_accounts_only_requires_private_video_flag(self):
        user = SimpleNamespace(
            is_superuser=False,
            is_authenticated=True,
            can_view_private_videos=True,
            has_perm=lambda perm: False,
        )

        self.video.permissions = Video.ACCOUNTS_ONLY
        self.assertTrue(has_perm(user, self.video))

    def test_unauthenticated_user_blocked_unless_public(self):
        user = SimpleNamespace(
            is_superuser=False,
            is_authenticated=False,
            can_view_private_videos=False,
            has_perm=lambda perm: False,
        )

        self.video.permissions = Video.DEBATERS_IN_ROUND
        self.assertFalse(has_perm(user, self.video))

        self.video.permissions = Video.ALL
        self.assertTrue(has_perm(user, self.video))
