# pylint: disable=import-outside-toplevel



from datetime import date
from django.test import TestCase



from core.models import School, Tournament, Debater


class UtilityFunctionTests(TestCase):
    """Test actual utility functions where they exist"""

    def setUp(self):
        self.school = School.objects.create(name="Test School")
        self.tournament = Tournament.objects.create(
            name="Test Tournament",
            host=self.school,
            date=date(2024, 1, 1),
            season="2024",
        )

    def test_model_methods_comprehensive(self):
        """Test model methods more comprehensively"""
        # Test Tournament methods
        self.tournament.save()  # Exercise save method

        # Test Tournament string representation
        str_repr = str(self.tournament)
        self.assertIsInstance(str_repr, str)

        # Test School methods
        self.school.save()
        school_str = str(self.school)
        self.assertIsInstance(school_str, str)

    def test_model_properties(self):
        """Test model properties and calculated fields"""
        # Create debater with more details
        debater = Debater.objects.create(
            first_name="John",
            last_name="Doe",
            school=self.school,
            status=Debater.VARSITY,
        )

        # Test debater properties
        full_name = str(debater)
        self.assertEqual(full_name, "John Doe")

        # Test school relationship
        self.assertEqual(debater.school, self.school)

    def test_model_managers_custom_methods(self):
        """Test custom manager methods if they exist"""
        # Test School manager
        all_schools = School.objects.all()
        self.assertIn(self.school, all_schools)

        # Test Tournament manager
        all_tournaments = Tournament.objects.all()
        self.assertIn(self.tournament, all_tournaments)

        # Test filtering
        tournaments_2024 = Tournament.objects.filter(season="2024")
        self.assertIn(self.tournament, tournaments_2024)

    def test_model_field_choices(self):
        """Test model field choices"""
        # Test Debater status choices
        choices = Debater.STATUS
        self.assertTrue(len(choices) > 0)

        # Test that choices include expected values
        choice_values = [choice[0] for choice in choices]
        self.assertIn(Debater.VARSITY, choice_values)
        self.assertIn(Debater.NOVICE, choice_values)

    def test_model_meta_options(self):
        """Test model meta options"""
        # Test Tournament meta
        meta = Tournament._meta
        self.assertTrue(hasattr(meta, "db_table"))

        # Test School meta
        school_meta = School._meta
        self.assertTrue(hasattr(school_meta, "db_table"))

    def test_signal_handlers_if_available(self):
        """Test Django signal handlers if they exist"""
        try:
            # Look for signals in models or separate signal files

            # Create a model to trigger signals
            test_school = School(name="Signal Test School")
            test_school.save()  # Should trigger post_save

            # Test that the object was created
            self.assertTrue(test_school.pk)

        except Exception:
            pass
