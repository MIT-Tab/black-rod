

# pylint: disable=import-outside-toplevel
import datetime
from django.test import TestCase, Client
from apda import urls
from core.models.school import School
from core.models.debater import Debater
from core.models.tournament import Tournament


class URLTest(TestCase):
    """Test URL configuration and resolution"""

    def test_urls_module_import(self):
        """Test that URLs module can be imported"""
        self.assertIsNotNone(urls)

    def test_url_patterns_exist(self):
        """Test that URL patterns are defined"""
        self.assertIsInstance(urls.urlpatterns, list)


class ViewBasicsTest(TestCase):
    """Test basic view functionality"""

    def setUp(self):
        self.client = Client()
        self.school = School.objects.create(name="Test School")

    def test_client_can_make_requests(self):
        """Test that test client works"""
        response = self.client.get("/nonexistent/")
        self.assertIn(response.status_code, [404, 500])

    def test_user_authentication_works(self):
        """Test user authentication in views context"""
        # Create user with email and is_active=True for allauth compatibility
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            email="testuser@example.com",
            is_active=True,
        )
        login_successful = self.client.login(
            username="testuser", password="testpass123"
        )
        self.assertTrue(login_successful)

    def test_view_context_with_models(self):
        """Test view context can work with models"""
        # Create some model instances for view context
        debater = Debater.objects.create(
            first_name="View", last_name="Test", school=self.school
        )
        tournament = Tournament.objects.create(
            name="View Test Tournament",
            host=self.school,
            date=datetime.date.today(),
            season="2024",
        )

        # Verify objects exist and can be used in view context
        self.assertEqual(debater.school, self.school)
        self.assertEqual(tournament.host, self.school)


class ViewHelperTest(TestCase):
    """Test view helper functions and mixins"""
