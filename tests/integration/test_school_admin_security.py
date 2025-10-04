from datetime import date

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from core.models import Debater, School, SchoolAdmin

User = get_user_model()


class SchoolAdminSecurityTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.school1 = School.objects.create(name="School One")
        self.school2 = School.objects.create(name="School Two")
        
        self.admin_user = User.objects.create_user(
            username="schooladmin",
            email="admin@school.com",
            password="pass123"
        )
        SchoolAdmin.objects.create(user=self.admin_user, school=self.school1)
        
        self.regular_user = User.objects.create_user(
            username="regular",
            email="regular@example.com",
            password="pass123"
        )
        
        self.superuser = User.objects.create_superuser(
            username="super",
            email="super@example.com",
            password="superpass"
        )
        
        current_year = int(settings.CURRENT_SEASON)
        self.debater1 = Debater.objects.create(
            first_name="Test",
            last_name="One",
            school=self.school1,
            first_season=str(current_year),
            latest_season=str(current_year)
        )
        self.debater2 = Debater.objects.create(
            first_name="Test",
            last_name="Two",
            school=self.school2,
            first_season=str(current_year),
            latest_season=str(current_year)
        )

    def test_unauthenticated_cannot_access_dashboard(self):
        response = self.client.get(reverse('core:school_admin_dashboard'))
        self.assertEqual(response.status_code, 302)

    def test_regular_user_cannot_access_dashboard(self):
        self.client.login(username='regular', password='pass123')
        response = self.client.get(reverse('core:school_admin_dashboard'))
        self.assertEqual(response.status_code, 403)

    def test_regular_user_cannot_edit_any_debater(self):
        self.client.login(username='regular', password='pass123')
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[self.debater1.id])
        )
        self.assertEqual(response.status_code, 403)

    def test_regular_user_cannot_create_debater(self):
        self.client.login(username='regular', password='pass123')
        response = self.client.get(
            reverse('core:school_admin_debater_create', args=[self.school1.id])
        )
        self.assertEqual(response.status_code, 403)

    def test_school_admin_cannot_edit_other_school_debater(self):
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[self.debater2.id])
        )
        self.assertEqual(response.status_code, 404)

    def test_school_admin_cannot_create_for_other_school(self):
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.get(
            reverse('core:school_admin_debater_create', args=[self.school2.id])
        )
        self.assertEqual(response.status_code, 403)

    def test_school_admin_can_edit_own_school_debater(self):
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[self.debater1.id])
        )
        self.assertEqual(response.status_code, 200)

    def test_school_admin_can_create_for_own_school(self):
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.get(
            reverse('core:school_admin_debater_create', args=[self.school1.id])
        )
        self.assertEqual(response.status_code, 200)

    def test_superuser_without_school_admin_cannot_access_dashboard(self):
        self.client.login(username='super', password='superpass')
        response = self.client.get(reverse('core:school_admin_dashboard'))
        self.assertEqual(response.status_code, 403)

    def test_superuser_without_school_admin_cannot_edit_debater(self):
        self.client.login(username='super', password='superpass')
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[self.debater1.id])
        )
        self.assertEqual(response.status_code, 403)

    def test_superuser_with_school_admin_can_edit_debater(self):
        SchoolAdmin.objects.create(user=self.superuser, school=self.school1)
        self.client.login(username='super', password='superpass')
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[self.debater1.id])
        )
        self.assertEqual(response.status_code, 200)

    def test_cannot_post_edit_without_permission(self):
        self.client.login(username='regular', password='pass123')
        response = self.client.post(
            reverse('core:school_admin_debater_update', args=[self.debater1.id]),
            {
                'first_name': 'Hacked',
                'last_name': 'Name',
                'status': Debater.VARSITY,
                'first_season': settings.CURRENT_SEASON,
                'latest_season': settings.CURRENT_SEASON,
            }
        )
        self.assertEqual(response.status_code, 403)
        self.debater1.refresh_from_db()
        self.assertNotEqual(self.debater1.first_name, 'Hacked')

    def test_cannot_post_create_without_permission(self):
        self.client.login(username='regular', password='pass123')
        response = self.client.post(
            reverse('core:school_admin_debater_create', args=[self.school1.id]),
            {
                'first_name': 'Unauthorized',
                'last_name': 'Debater',
                'status': Debater.VARSITY,
                'first_season': settings.CURRENT_SEASON,
                'latest_season': settings.CURRENT_SEASON,
            }
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            Debater.objects.filter(
                first_name='Unauthorized',
                last_name='Debater'
            ).exists()
        )

    def test_school_admin_cannot_post_edit_other_school(self):
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.post(
            reverse('core:school_admin_debater_update', args=[self.debater2.id]),
            {
                'first_name': 'Changed',
                'last_name': 'Name',
                'status': Debater.VARSITY,
                'first_season': settings.CURRENT_SEASON,
                'latest_season': settings.CURRENT_SEASON,
            }
        )
        self.assertEqual(response.status_code, 404)
        self.debater2.refresh_from_db()
        self.assertNotEqual(self.debater2.first_name, 'Changed')

    def test_school_admin_cannot_change_school_in_edit(self):
        self.client.login(username='schooladmin', password='pass123')
        original_school = self.debater1.school
        response = self.client.post(
            reverse('core:school_admin_debater_update', args=[self.debater1.id]),
            {
                'first_name': 'Test',
                'last_name': 'One',
                'status': Debater.VARSITY,
                'first_season': settings.CURRENT_SEASON,
                'latest_season': settings.CURRENT_SEASON,
            }
        )
        self.debater1.refresh_from_db()
        self.assertEqual(self.debater1.school, original_school)

    def test_old_debater_not_visible_in_dashboard(self):
        current_year = int(settings.CURRENT_SEASON)
        old_debater = Debater.objects.create(
            first_name="Old",
            last_name="Debater",
            school=self.school1,
            first_season=str(current_year - 10),
            latest_season=str(current_year - 7)
        )
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.get(reverse('core:school_admin_dashboard'))
        self.assertNotContains(response, "Old Debater")

    def test_old_debater_cannot_be_edited(self):
        current_year = int(settings.CURRENT_SEASON)
        old_debater = Debater.objects.create(
            first_name="Old",
            last_name="Debater",
            school=self.school1,
            first_season=str(current_year - 10),
            latest_season=str(current_year - 7)
        )
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[old_debater.id])
        )
        self.assertEqual(response.status_code, 404)

    def test_cannot_bypass_school_check_with_direct_url(self):
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.post(
            f'/core/school-admin/debater/{self.debater2.id}/edit/',
            {
                'first_name': 'Bypass',
                'last_name': 'Attempt',
                'status': Debater.VARSITY,
                'first_season': settings.CURRENT_SEASON,
                'latest_season': settings.CURRENT_SEASON,
            }
        )
        self.assertEqual(response.status_code, 404)
        self.debater2.refresh_from_db()
        self.assertNotEqual(self.debater2.first_name, 'Bypass')

    def test_daily_limit_enforced_per_user(self):
        cache.clear()
        self.client.login(username='schooladmin', password='pass123')
        
        for i in range(5):
            response = self.client.post(
                reverse('core:school_admin_debater_create', args=[self.school1.id]),
                {
                    'first_name': f'User{i}',
                    'last_name': 'Test',
                    'status': Debater.VARSITY,
                    'first_season': settings.CURRENT_SEASON,
                    'latest_season': settings.CURRENT_SEASON,
                }
            )
        
        response = self.client.post(
            reverse('core:school_admin_debater_create', args=[self.school1.id]),
            {
                'first_name': 'SixthUser',
                'last_name': 'Test',
                'status': Debater.VARSITY,
                'first_season': settings.CURRENT_SEASON,
                'latest_season': settings.CURRENT_SEASON,
            }
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            Debater.objects.filter(first_name='SixthUser').exists()
        )


class SchoolAdminPermissionEdgeCasesTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.school = School.objects.create(name="Test School")
        current_year = int(settings.CURRENT_SEASON)
        self.debater = Debater.objects.create(
            first_name="Test",
            last_name="Debater",
            school=self.school,
            first_season=str(current_year),
            latest_season=str(current_year)
        )

    def test_deleted_school_admin_loses_access(self):
        user = User.objects.create_user(
            username="tempadmin",
            email="temp@school.com",
            password="pass123"
        )
        admin = SchoolAdmin.objects.create(user=user, school=self.school)
        
        self.client.login(username='tempadmin', password='pass123')
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[self.debater.id])
        )
        self.assertEqual(response.status_code, 200)
        
        admin.delete()
        
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[self.debater.id])
        )
        self.assertEqual(response.status_code, 403)

    def test_school_admin_for_different_school_has_no_access(self):
        other_school = School.objects.create(name="Other School")
        user = User.objects.create_user(
            username="otheradmin",
            email="other@school.com",
            password="pass123"
        )
        SchoolAdmin.objects.create(user=user, school=other_school)
        
        self.client.login(username='otheradmin', password='pass123')
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[self.debater.id])
        )
        self.assertEqual(response.status_code, 404)

    def test_inactive_user_cannot_access(self):
        user = User.objects.create_user(
            username="inactive",
            email="inactive@school.com",
            password="pass123",
            is_active=False
        )
        SchoolAdmin.objects.create(user=user, school=self.school)
        
        login_success = self.client.login(username='inactive', password='pass123')
        self.assertFalse(login_success)
