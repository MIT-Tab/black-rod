from datetime import date, timedelta

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from core.models import Debater, School, SchoolAdmin

User = get_user_model()


class SchoolAdminModelTest(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="Test School")
        self.user = User.objects.create_user(
            username="testadmin",
            email="testadmin@example.com",
            password="testpass123"
        )

    def test_school_admin_creation(self):
        admin = SchoolAdmin.objects.create(user=self.user, school=self.school)
        self.assertEqual(admin.user, self.user)
        self.assertEqual(admin.school, self.school)
        self.assertIsNotNone(admin.created_at)

    def test_school_admin_string_representation(self):
        admin = SchoolAdmin.objects.create(user=self.user, school=self.school)
        self.assertEqual(str(admin), f"{self.user.username} - {self.school.name}")

    def test_school_admin_unique_together(self):
        SchoolAdmin.objects.create(user=self.user, school=self.school)
        with self.assertRaises(Exception):
            SchoolAdmin.objects.create(user=self.user, school=self.school)

    def test_school_admin_multiple_schools(self):
        school2 = School.objects.create(name="Another School")
        admin1 = SchoolAdmin.objects.create(user=self.user, school=self.school)
        admin2 = SchoolAdmin.objects.create(user=self.user, school=school2)
        self.assertEqual(SchoolAdmin.objects.filter(user=self.user).count(), 2)

    def test_school_multiple_admins(self):
        user2 = User.objects.create_user(
            username="admin2",
            email="admin2@example.com",
            password="pass123"
        )
        admin1 = SchoolAdmin.objects.create(user=self.user, school=self.school)
        admin2 = SchoolAdmin.objects.create(user=user2, school=self.school)
        self.assertEqual(SchoolAdmin.objects.filter(school=self.school).count(), 2)


class SchoolAdminDashboardViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.school = School.objects.create(name="Test School")
        self.admin_user = User.objects.create_user(
            username="schooladmin",
            email="admin@school.com",
            password="pass123"
        )
        self.school_admin = SchoolAdmin.objects.create(
            user=self.admin_user,
            school=self.school
        )
        self.superuser = User.objects.create_superuser(
            username="super",
            email="super@example.com",
            password="superpass"
        )
        
        current_year = int(settings.CURRENT_SEASON)
        self.debater_recent = Debater.objects.create(
            first_name="Recent",
            last_name="Debater",
            school=self.school,
            first_season=str(current_year - 2),
            latest_season=str(current_year)
        )
        self.debater_old = Debater.objects.create(
            first_name="Old",
            last_name="Debater",
            school=self.school,
            first_season=str(current_year - 10),
            latest_season=str(current_year - 7)
        )

    def test_dashboard_requires_authentication(self):
        response = self.client.get(reverse('core:school_admin_dashboard'))
        self.assertEqual(response.status_code, 302)

    def test_dashboard_accessible_by_school_admin(self):
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.get(reverse('core:school_admin_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "School Admin Dashboard")

    def test_dashboard_shows_recent_debaters_only(self):
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.get(reverse('core:school_admin_dashboard'))
        self.assertContains(response, "Recent Debater")
        self.assertNotContains(response, "Old Debater")

    def test_dashboard_accessible_by_superuser(self):
        SchoolAdmin.objects.create(user=self.superuser, school=self.school)
        self.client.login(username='super', password='superpass')
        response = self.client.get(reverse('core:school_admin_dashboard'))
        self.assertEqual(response.status_code, 200)

    def test_dashboard_not_accessible_by_regular_user(self):
        regular_user = User.objects.create_user(
            username="regular",
            email="regular@example.com",
            password="pass123"
        )
        self.client.login(username='regular', password='pass123')
        response = self.client.get(reverse('core:school_admin_dashboard'))
        self.assertEqual(response.status_code, 403)


class SchoolAdminDebaterUpdateTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.school = School.objects.create(name="Test School")
        self.admin_user = User.objects.create_user(
            username="schooladmin",
            email="admin@school.com",
            password="pass123"
        )
        self.school_admin = SchoolAdmin.objects.create(
            user=self.admin_user,
            school=self.school
        )
        
        current_year = int(settings.CURRENT_SEASON)
        self.debater = Debater.objects.create(
            first_name="Test",
            last_name="Debater",
            school=self.school,
            first_season=str(current_year - 2),
            latest_season=str(current_year),
            status=Debater.NOVICE
        )

    def test_update_debater_name(self):
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.post(
            reverse('core:school_admin_debater_update', args=[self.debater.id]),
            {
                'first_name': 'Updated',
                'last_name': 'Name',
                'status': Debater.NOVICE,
                'first_season': str(int(settings.CURRENT_SEASON) - 2),
                'latest_season': settings.CURRENT_SEASON,
            }
        )
        self.debater.refresh_from_db()
        self.assertEqual(self.debater.first_name, 'Updated')
        self.assertEqual(self.debater.last_name, 'Name')

    def test_update_debater_status(self):
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.post(
            reverse('core:school_admin_debater_update', args=[self.debater.id]),
            {
                'first_name': 'Test',
                'last_name': 'Debater',
                'status': Debater.VARSITY,
                'first_season': str(int(settings.CURRENT_SEASON) - 2),
                'latest_season': settings.CURRENT_SEASON,
            }
        )
        self.debater.refresh_from_db()
        self.assertEqual(self.debater.status, Debater.VARSITY)

    def test_cannot_set_latest_season_too_old(self):
        self.client.login(username='schooladmin', password='pass123')
        current_year = int(settings.CURRENT_SEASON)
        too_old_season = str(current_year - 7)
        response = self.client.post(
            reverse('core:school_admin_debater_update', args=[self.debater.id]),
            {
                'first_name': 'Test',
                'last_name': 'Debater',
                'status': Debater.NOVICE,
                'first_season': str(current_year - 2),
                'latest_season': too_old_season,
            }
        )
        self.debater.refresh_from_db()
        self.assertNotEqual(self.debater.latest_season, too_old_season)

    def test_cannot_update_other_school_debater(self):
        other_school = School.objects.create(name="Other School")
        other_debater = Debater.objects.create(
            first_name="Other",
            last_name="Debater",
            school=other_school,
            first_season=settings.CURRENT_SEASON,
            latest_season=settings.CURRENT_SEASON
        )
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[other_debater.id])
        )
        self.assertEqual(response.status_code, 404)

    def test_cannot_update_old_debater(self):
        current_year = int(settings.CURRENT_SEASON)
        old_debater = Debater.objects.create(
            first_name="Old",
            last_name="Debater",
            school=self.school,
            first_season=str(current_year - 10),
            latest_season=str(current_year - 7)
        )
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[old_debater.id])
        )
        self.assertEqual(response.status_code, 404)


class SchoolAdminDebaterCreateTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.school = School.objects.create(name="Test School")
        self.admin_user = User.objects.create_user(
            username="schooladmin",
            email="admin@school.com",
            password="pass123"
        )
        self.school_admin = SchoolAdmin.objects.create(
            user=self.admin_user,
            school=self.school
        )
        cache.clear()

    def test_create_debater(self):
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.post(
            reverse('core:school_admin_debater_create', args=[self.school.id]),
            {
                'first_name': 'New',
                'last_name': 'Debater',
                'status': Debater.VARSITY,
                'first_season': settings.CURRENT_SEASON,
                'latest_season': settings.CURRENT_SEASON,
            }
        )
        self.assertTrue(
            Debater.objects.filter(
                first_name='New',
                last_name='Debater',
                school=self.school
            ).exists()
        )

    def test_created_debater_assigned_to_correct_school(self):
        self.client.login(username='schooladmin', password='pass123')
        self.client.post(
            reverse('core:school_admin_debater_create', args=[self.school.id]),
            {
                'first_name': 'New',
                'last_name': 'Debater',
                'status': Debater.VARSITY,
                'first_season': settings.CURRENT_SEASON,
                'latest_season': settings.CURRENT_SEASON,
            }
        )
        debater = Debater.objects.get(first_name='New', last_name='Debater')
        self.assertEqual(debater.school, self.school)

    def test_cannot_create_debater_for_other_school(self):
        other_school = School.objects.create(name="Other School")
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.get(
            reverse('core:school_admin_debater_create', args=[other_school.id])
        )
        self.assertEqual(response.status_code, 403)

    def test_daily_limit_enforced(self):
        self.client.login(username='schooladmin', password='pass123')
        
        for i in range(5):
            response = self.client.post(
                reverse('core:school_admin_debater_create', args=[self.school.id]),
                {
                    'first_name': f'Debater{i}',
                    'last_name': 'Test',
                    'status': Debater.VARSITY,
                    'first_season': settings.CURRENT_SEASON,
                    'latest_season': settings.CURRENT_SEASON,
                }
            )
        
        response = self.client.post(
            reverse('core:school_admin_debater_create', args=[self.school.id]),
            {
                'first_name': 'Excess',
                'last_name': 'Debater',
                'status': Debater.VARSITY,
                'first_season': settings.CURRENT_SEASON,
                'latest_season': settings.CURRENT_SEASON,
            }
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            Debater.objects.filter(first_name='Excess', last_name='Debater').exists()
        )

    def test_cannot_create_duplicate_debater(self):
        Debater.objects.create(
            first_name='Existing',
            last_name='Debater',
            school=self.school,
            first_season=settings.CURRENT_SEASON,
            latest_season=settings.CURRENT_SEASON
        )
        self.client.login(username='schooladmin', password='pass123')
        response = self.client.post(
            reverse('core:school_admin_debater_create', args=[self.school.id]),
            {
                'first_name': 'Existing',
                'last_name': 'Debater',
                'status': Debater.VARSITY,
                'first_season': settings.CURRENT_SEASON,
                'latest_season': settings.CURRENT_SEASON,
            }
        )
        self.assertEqual(
            Debater.objects.filter(first_name='Existing', last_name='Debater').count(),
            1
        )


class SchoolAdminPermissionsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.school1 = School.objects.create(name="School 1")
        self.school2 = School.objects.create(name="School 2")
        
        self.admin1 = User.objects.create_user(
            username="admin1",
            email="admin1@example.com",
            password="pass123"
        )
        self.admin2 = User.objects.create_user(
            username="admin2",
            email="admin2@example.com",
            password="pass123"
        )
        
        SchoolAdmin.objects.create(user=self.admin1, school=self.school1)
        SchoolAdmin.objects.create(user=self.admin2, school=self.school2)
        
        current_year = int(settings.CURRENT_SEASON)
        self.debater1 = Debater.objects.create(
            first_name="Debater",
            last_name="One",
            school=self.school1,
            first_season=str(current_year),
            latest_season=str(current_year)
        )
        self.debater2 = Debater.objects.create(
            first_name="Debater",
            last_name="Two",
            school=self.school2,
            first_season=str(current_year),
            latest_season=str(current_year)
        )

    def test_admin_can_only_edit_own_school_debaters(self):
        self.client.login(username='admin1', password='pass123')
        
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[self.debater1.id])
        )
        self.assertEqual(response.status_code, 200)
        
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[self.debater2.id])
        )
        self.assertEqual(response.status_code, 404)

    def test_superuser_can_edit_all_debaters(self):
        superuser = User.objects.create_superuser(
            username="super",
            email="super@example.com",
            password="superpass"
        )
        SchoolAdmin.objects.create(user=superuser, school=self.school1)
        SchoolAdmin.objects.create(user=superuser, school=self.school2)
        self.client.login(username='super', password='superpass')
        
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[self.debater1.id])
        )
        self.assertEqual(response.status_code, 200)
        
        response = self.client.get(
            reverse('core:school_admin_debater_update', args=[self.debater2.id])
        )
        self.assertEqual(response.status_code, 200)


@pytest.mark.django_db
def test_school_admin_menu_validator():
    from core.menu_validators import is_school_admin
    from django.test import RequestFactory
    
    school = School.objects.create(name="Test School")
    admin_user = User.objects.create_user(
        username="admin",
        email="admin@example.com",
        password="pass123"
    )
    SchoolAdmin.objects.create(user=admin_user, school=school)
    
    regular_user = User.objects.create_user(
        username="regular",
        email="regular@example.com",
        password="pass123"
    )
    
    factory = RequestFactory()
    
    request = factory.get('/')
    request.user = admin_user
    assert is_school_admin(request) is True
    
    request.user = regular_user
    assert is_school_admin(request) is False


@pytest.mark.django_db
def test_season_validation():
    school = School.objects.create(name="Test School")
    current_year = int(settings.CURRENT_SEASON)
    six_years_ago = current_year - 6
    
    debater = Debater.objects.create(
        first_name="Test",
        last_name="Debater",
        school=school,
        first_season=str(current_year - 2),
        latest_season=str(current_year)
    )
    
    visible_debaters = Debater.objects.filter(
        school=school,
        latest_season__gte=str(six_years_ago)
    )
    assert debater in visible_debaters
    
    old_debater = Debater.objects.create(
        first_name="Old",
        last_name="Debater",
        school=school,
        first_season=str(current_year - 10),
        latest_season=str(current_year - 7)
    )
    
    visible_debaters = Debater.objects.filter(
        school=school,
        latest_season__gte=str(six_years_ago)
    )
    assert old_debater not in visible_debaters
