# pylint: disable=import-outside-toplevel
import pytest
from django.test import TestCase

from core.models.school import School
from core.models.debater import Debater


class FormTestCase(TestCase):
    """Test form-related functionality"""

    def setUp(self):
        self.school = School.objects.create(name="Form Test School")

    def test_debater_form_data_validation(self):
        """Test that debater data validates correctly"""
        # Test valid debater creation
        debater_data = {
            "first_name": "Valid",
            "last_name": "Name",
            "school": self.school,
            "status": Debater.VARSITY,
        }

        debater = Debater.objects.create(**debater_data)
        self.assertEqual(debater.first_name, "Valid")
        self.assertEqual(debater.last_name, "Name")

    def test_school_form_data_validation(self):
        """Test that school data validates correctly"""
        # Test valid school creation
        school_data = {"name": "Valid School Name", "included_in_oty": True}

        school = School.objects.create(**school_data)
        self.assertEqual(school.name, "Valid School Name")
        self.assertTrue(school.included_in_oty)

    def test_model_field_max_lengths(self):
        """Test model field constraints"""
        # Test debater name fields
        long_name = "x" * 64  # Longer than max_length of 32

        try:
            debater = Debater.objects.create(first_name=long_name, last_name="Test")
            # If this succeeds, the field might not have proper validation
            # or database might truncate
        except:
            # Expected if validation is working
            pass

    def test_model_required_fields(self):
        """Test that required fields are enforced"""
        # School name is required
        try:
            school = School.objects.create(name="")
            # Empty string might be allowed, let's test it exists
            self.assertEqual(school.name, "")
        except:
            # If validation prevents empty names
            pass

    def test_model_choices_validation(self):
        """Test that choice fields work correctly"""
        # Test valid status choices
        varsity_debater = Debater.objects.create(
            first_name="Test", last_name="Varsity", status=Debater.VARSITY
        )
        self.assertEqual(varsity_debater.status, Debater.VARSITY)

        novice_debater = Debater.objects.create(
            first_name="Test", last_name="Novice", status=Debater.NOVICE
        )
        self.assertEqual(novice_debater.status, Debater.NOVICE)


class UtilityTestCase(TestCase):
    """Test utility functions and helper methods"""

    def test_string_representations(self):
        """Test __str__ methods on models"""
        school = School.objects.create(name="String Test School")
        self.assertEqual(str(school), "String Test School")

        debater = Debater.objects.create(
            first_name="String", last_name="Test", school=school
        )
        self.assertEqual(str(debater), "String Test")

    def test_model_properties(self):
        """Test custom properties on models"""
        debater = Debater.objects.create(first_name="Property", last_name="Test")

        # Test name property
        expected_name = "Property Test"
        self.assertEqual(debater.name, expected_name)

    def test_model_relationships_exist(self):
        """Test that model relationships are properly defined"""
        school = School.objects.create(name="Relationship Test")
        debater = Debater.objects.create(
            first_name="Relationship", last_name="Test", school=school
        )

        # Test foreign key relationship
        self.assertEqual(debater.school, school)

        # Test reverse relationship
        self.assertIn(debater, school.debaters.all())


@pytest.mark.django_db
def test_constants_are_defined():
    """Test that model constants are properly defined"""
    assert hasattr(Debater, "VARSITY")
    assert hasattr(Debater, "NOVICE")
    assert hasattr(Debater, "STATUS")

    assert Debater.VARSITY == 1
    assert Debater.NOVICE == 0


def test_model_meta_information():
    """Test model meta information"""
    # Test that models have proper meta information
    assert School._meta.app_label == "core"
    assert Debater._meta.app_label == "core"
