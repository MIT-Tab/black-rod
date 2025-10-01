
from datetime import date
from django.test import TestCase
from django.contrib.admin.sites import AdminSite


from core.models.school import School
from core.models.debater import Debater
from core.models.tournament import Tournament
from core.models.user import User



class MockRequest:
    """Mock request object for admin testing"""

    def __init__(self, user=None):
        self.user = user


class AdminTestCase(TestCase):
    """Test admin interface functionality"""

    def setUp(self):
        self.site = AdminSite()
        self.superuser = User.objects.create_superuser(
            username="admin", email="admin@test.com", password="admin123"
        )



    def test_model_string_representations_for_admin(self):
        """Test model __str__ methods work for admin display"""
        school = School.objects.create(name="Admin Test School")
        self.assertEqual(str(school), "Admin Test School")

        debater = Debater.objects.create(
            first_name="Admin", last_name="Test", school=school
        )
        self.assertEqual(str(debater), "Admin Test")

        tournament = Tournament.objects.create(
            name="Admin Test Tournament", host=school, date=date.today()
        )
        # Tournament name gets set to host name during save
        self.assertEqual(str(tournament), "Admin Test School")

    def test_admin_list_display_fields_exist(self):
        """Test that common admin list display fields exist on models"""
        school = School.objects.create(name="Field Test School")

        # Test that commonly used admin fields exist
        self.assertTrue(hasattr(school, "name"))
        self.assertTrue(hasattr(school, "included_in_oty"))

        debater = Debater.objects.create(
            first_name="Field", last_name="Test", school=school
        )

        self.assertTrue(hasattr(debater, "first_name"))
        self.assertTrue(hasattr(debater, "last_name"))
        self.assertTrue(hasattr(debater, "school"))
        self.assertTrue(hasattr(debater, "status"))

    def test_admin_search_fields_work(self):
        """Test that potential admin search fields work"""
        school = School.objects.create(name="Search Test School")

        # Test searching by name
        schools = School.objects.filter(name__icontains="Search")
        self.assertIn(school, schools)

        debater = Debater.objects.create(
            first_name="Search", last_name="Test", school=school
        )

        # Test searching by first name
        debaters = Debater.objects.filter(first_name__icontains="Search")
        self.assertIn(debater, debaters)

        # Test searching by last name
        debaters = Debater.objects.filter(last_name__icontains="Test")
        self.assertIn(debater, debaters)

    def test_admin_filter_fields_work(self):
        """Test that potential admin filter fields work"""
        school1 = School.objects.create(name="Filter School 1", included_in_oty=True)
        school2 = School.objects.create(name="Filter School 2", included_in_oty=False)

        # Test filtering by included_in_oty
        oty_schools = School.objects.filter(included_in_oty=True)
        self.assertIn(school1, oty_schools)
        self.assertNotIn(school2, oty_schools)

        debater1 = Debater.objects.create(
            first_name="Filter1",
            last_name="Test",
            school=school1,
            status=Debater.VARSITY,
        )
        debater2 = Debater.objects.create(
            first_name="Filter2",
            last_name="Test",
            school=school2,
            status=Debater.NOVICE,
        )

        # Test filtering by status
        varsity_debaters = Debater.objects.filter(status=Debater.VARSITY)
        self.assertIn(debater1, varsity_debaters)
        self.assertNotIn(debater2, varsity_debaters)

        # Test filtering by school
        school1_debaters = Debater.objects.filter(school=school1)
        self.assertIn(debater1, school1_debaters)
        self.assertNotIn(debater2, school1_debaters)





def test_model_verbose_names():
    """Test model verbose names for admin display"""
    # Test that models have reasonable verbose names
    assert School._meta.verbose_name
    assert Debater._meta.verbose_name
    assert Tournament._meta.verbose_name
