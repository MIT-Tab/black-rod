# pylint: disable=import-outside-toplevel
"""
Unit tests for API serializers.
"""

from django.test import TestCase
from core.models.school import School
from core.models.debater import Debater
from api.serializers import serialize_school, serialize_debater


class APISchoolSerializerTest(TestCase):
    """Test school serialization"""
    
    def test_serialize_school(self):
        """Test basic school serialization"""
        school = School.objects.create(name="Harvard University")
        serialized = serialize_school(school)
        
        self.assertEqual(serialized["id"], school.id)
        self.assertEqual(serialized["name"], "Harvard University")
        self.assertIn("id", serialized)
        self.assertIn("name", serialized)


class APIDebaterSerializerTest(TestCase):
    """Test debater serialization"""
    
    def setUp(self):
        self.school = School.objects.create(name="MIT")
    
    def test_serialize_debater_with_school(self):
        """Test debater serialization with school"""
        debater = Debater.objects.create(
            first_name="John",
            last_name="Doe",
            school=self.school,
            status=Debater.VARSITY
        )
        serialized = serialize_debater(debater)
        
        self.assertEqual(serialized["id"], debater.id)
        self.assertEqual(serialized["name"], "John Doe")
        self.assertEqual(serialized["first_name"], "John")
        self.assertEqual(serialized["last_name"], "Doe")
        self.assertEqual(serialized["status"], "Varsity")
        self.assertEqual(serialized["school_id"], self.school.id)
        self.assertEqual(serialized["school_name"], "MIT")
    
    def test_serialize_debater_without_school(self):
        """Test debater serialization without school"""
        debater = Debater.objects.create(
            first_name="Jane",
            last_name="Smith",
            status=Debater.NOVICE
        )
        serialized = serialize_debater(debater)
        
        self.assertEqual(serialized["id"], debater.id)
        self.assertEqual(serialized["name"], "Jane Smith")
        self.assertEqual(serialized["status"], "Novice")
        self.assertIsNone(serialized["school_id"])
        self.assertIsNone(serialized["school_name"])
    
    def test_serialize_novice_debater(self):
        """Test novice status serialization"""
        debater = Debater.objects.create(
            first_name="Novice",
            last_name="Player",
            school=self.school,
            status=Debater.NOVICE
        )
        serialized = serialize_debater(debater)
        
        self.assertEqual(serialized["status"], "Novice")
