# pylint: disable=import-outside-toplevel

from datetime import date
from django.test import TestCase

from core.models.school import School, SchoolLookup
from core.models.debater import Debater
from core.models.tournament import Tournament


class DatabaseTestCase(TestCase):
    """Test database operations and constraints"""

    def test_school_unique_constraint(self):
        """Test that school names must be unique"""
        School.objects.create(name="Unique Test School")

        # Attempting to create another school with same name should fail
        with self.assertRaises(Exception):  # Could be IntegrityError or ValidationError
            School.objects.create(name="Unique Test School")

    def test_school_lookup_unique_constraint(self):
        """Test that school lookup server names must be unique"""
        school1 = School.objects.create(name="School 1")
        school2 = School.objects.create(name="School 2")

        SchoolLookup.objects.create(server_name="test.edu", school=school1)

        # Attempting to create another lookup with same server name should fail
        with self.assertRaises(Exception):
            SchoolLookup.objects.create(server_name="test.edu", school=school2)

    def test_foreign_key_relationships(self):
        """Test foreign key relationships work correctly"""
        school = School.objects.create(name="FK Test School")

        # Test debater -> school relationship
        debater = Debater.objects.create(
            first_name="FK", last_name="Test", school=school
        )
        self.assertEqual(debater.school, school)

        # Test tournament -> school relationship
        tournament = Tournament.objects.create(
            name="FK Test Tournament", host=school, date=date.today(), season="2024"
        )
        self.assertEqual(tournament.host, school)

        # Test school lookup -> school relationship
        lookup = SchoolLookup.objects.create(server_name="fktest.edu", school=school)
        self.assertEqual(lookup.school, school)

    def test_cascade_behaviors(self):
        """Test cascade behaviors when related objects are deleted"""
        school = School.objects.create(name="Cascade Test School")

        # Create related objects
        debater = Debater.objects.create(
            first_name="Cascade", last_name="Test", school=school
        )

        tournament = Tournament.objects.create(
            name="Cascade Test Tournament",
            host=school,
            date=date.today(),
            season="2024",
        )

        lookup = SchoolLookup.objects.create(server_name="cascade.edu", school=school)

        # Delete school
        school.delete()

        # Check cascade behaviors
        debater.refresh_from_db()
        self.assertIsNone(debater.school)  # Should be SET_NULL

        tournament.refresh_from_db()
        self.assertIsNone(tournament.host)  # Should be SET_NULL

        # SchoolLookup should be CASCADE deleted
        with self.assertRaises(SchoolLookup.DoesNotExist):
            lookup.refresh_from_db()

    def test_model_save_methods(self):
        """Test custom save methods work correctly"""
        school = School.objects.create(name="Save Test School")
        debater = Debater.objects.create(
            first_name="Save", last_name="Test", school=school
        )

        # Test that save method executes without error
        debater.save()

        # Refresh from database
        debater.refresh_from_db()
        self.assertEqual(debater.first_name, "Save")

    def test_model_defaults(self):
        """Test that model default values work correctly"""
        # Test school defaults
        school = School.objects.create(name="Default Test School")
        self.assertTrue(school.included_in_oty)  # Should default to True

        # Test debater defaults
        debater = Debater.objects.create(first_name="Default", last_name="Test")
        self.assertEqual(debater.status, Debater.VARSITY)  # Should default to VARSITY

        # Test tournament defaults
        tournament = Tournament(
            name="Default Test Tournament",
            date=date.today(),
            season="2024",
            manual_name="Default Test Tournament",
            host=school,
        )
        tournament.save()
        self.assertEqual(tournament.num_rounds, 5)  # Should default to 5


class QueryTestCase(TestCase):
    """Test database queries and filtering"""

    def setUp(self):
        self.school1 = School.objects.create(
            name="Query School 1", included_in_oty=True
        )
        self.school2 = School.objects.create(
            name="Query School 2", included_in_oty=False
        )

        self.debater1 = Debater.objects.create(
            first_name="Query1",
            last_name="Test",
            school=self.school1,
            status=Debater.VARSITY,
        )
        self.debater2 = Debater.objects.create(
            first_name="Query2",
            last_name="Test",
            school=self.school2,
            status=Debater.NOVICE,
        )

    def test_school_filtering(self):
        """Test filtering schools"""
        oty_schools = School.objects.filter(included_in_oty=True)
        self.assertIn(self.school1, oty_schools)
        self.assertNotIn(self.school2, oty_schools)

        non_oty_schools = School.objects.filter(included_in_oty=False)
        self.assertIn(self.school2, non_oty_schools)
        self.assertNotIn(self.school1, non_oty_schools)

    def test_debater_filtering(self):
        """Test filtering debaters"""
        varsity_debaters = Debater.objects.filter(status=Debater.VARSITY)
        self.assertIn(self.debater1, varsity_debaters)
        self.assertNotIn(self.debater2, varsity_debaters)

        novice_debaters = Debater.objects.filter(status=Debater.NOVICE)
        self.assertIn(self.debater2, novice_debaters)
        self.assertNotIn(self.debater1, novice_debaters)

    def test_related_queries(self):
        """Test queries across relationships"""
        # Find debaters from specific school
        school1_debaters = Debater.objects.filter(school=self.school1)
        self.assertIn(self.debater1, school1_debaters)
        self.assertNotIn(self.debater2, school1_debaters)

        # Find schools with varsity debaters
        schools_with_varsity = School.objects.filter(debaters__status=Debater.VARSITY)
        self.assertIn(self.school1, schools_with_varsity)
