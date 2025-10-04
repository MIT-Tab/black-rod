# pylint: disable=import-outside-toplevel
"""
Integration tests for API views.
"""

import json
from django.test import TestCase, Client
from django.conf import settings
from django.core.cache import cache
from core.models.school import School
from core.models.debater import Debater


class APIActiveSchoolListViewTest(TestCase):
    """Test the active schools API endpoint"""
    
    def setUp(self):
        self.client = Client()
        self.current_year = int(settings.CURRENT_SEASON)
        cache.clear()  # Clear cache before each test
        
        # Create schools with different activity levels
        self.active_school1 = School.objects.create(name="Active University A")
        self.active_school2 = School.objects.create(name="Active University B")
        self.inactive_school = School.objects.create(name="Inactive University")
        
        # Create active debaters (within last 2 years)
        # School 1 has 3 active debaters
        for i in range(3):
            Debater.objects.create(
                first_name="Student",
                last_name=f"One-{i}",
                school=self.active_school1,
                latest_season=str(self.current_year)
            )
        
        # School 2 has 1 active debater
        Debater.objects.create(
            first_name="Student",
            last_name="Two",
            school=self.active_school2,
            latest_season=str(self.current_year - 1)
        )
        
        # Create inactive debater (more than 2 years ago)
        Debater.objects.create(
            first_name="Old",
            last_name="Graduate",
            school=self.inactive_school,
            latest_season=str(self.current_year - 3)
        )
    
    def test_active_schools_only(self):
        """Test that only schools with recent debaters are shown"""
        response = self.client.get('/api/schools/')
        
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        
        # Should only show 2 active schools
        self.assertEqual(data['count'], 2)
        
        school_names = [s['name'] for s in data['schools']]
        self.assertIn("Active University A", school_names)
        self.assertIn("Active University B", school_names)
        self.assertNotIn("Inactive University", school_names)
    
    def test_schools_ordered_by_activity(self):
        """Test that schools are ordered by number of active debaters"""
        response = self.client.get('/api/schools/')
        data = json.loads(response.content)
        
        schools = data['schools']
        # School with 3 debaters should come before school with 1
        self.assertEqual(schools[0]['name'], "Active University A")
        self.assertEqual(schools[1]['name'], "Active University B")
    
    def test_limit_to_25_schools(self):
        """Test that only top 25 schools are returned"""
        # Create 30 schools with varying activity
        for i in range(30):
            school = School.objects.create(name=f"School {i:02d}")
            # Each gets i+1 debaters
            for j in range(i + 1):
                Debater.objects.create(
                    first_name=f"Student",
                    last_name=f"{i}-{j}",
                    school=school,
                    latest_season=str(self.current_year)
                )
        
        response = self.client.get('/api/schools/')
        data = json.loads(response.content)
        
        # Should only return 25 schools (plus our 2 original active schools = 27 total active)
        # But limit to 25
        self.assertEqual(data['count'], 25)
        self.assertEqual(len(data['schools']), 25)
    
    def test_schools_have_required_fields(self):
        """Test that each school has id and name"""
        response = self.client.get('/api/schools/')
        data = json.loads(response.content)
        
        for school in data['schools']:
            self.assertIn('id', school)
            self.assertIn('name', school)
    
    def test_empty_when_no_active_schools(self):
        """Test endpoint with no active schools"""
        Debater.objects.all().delete()
        cache.clear()  # Clear cache after deleting data
        
        response = self.client.get('/api/schools/')
        data = json.loads(response.content)
        
        self.assertEqual(data['count'], 0)
        self.assertEqual(len(data['schools']), 0)
    
    def test_response_is_cached(self):
        """Test that the response is cached for 5 minutes"""
        # First request - should hit database
        response1 = self.client.get('/api/schools/')
        data1 = json.loads(response1.content)
        
        # Create a new school that should be active
        new_school = School.objects.create(name="New School")
        Debater.objects.create(
            first_name="New",
            last_name="Student",
            school=new_school,
            latest_season=str(self.current_year)
        )
        
        # Second request - should return cached data (not include new school)
        response2 = self.client.get('/api/schools/')
        data2 = json.loads(response2.content)
        
        # Data should be identical (cached)
        self.assertEqual(data1['count'], data2['count'])
        self.assertEqual(len(data1['schools']), len(data2['schools']))
        
        # Clear cache and try again
        cache.clear()
        response3 = self.client.get('/api/schools/')
        data3 = json.loads(response3.content)
        
        # Now should include the new school
        self.assertEqual(data3['count'], 3)
        school_names = [s['name'] for s in data3['schools']]
        self.assertIn("New School", school_names)


class APIAllSchoolListViewTest(TestCase):
    """Test the all schools API endpoint"""
    
    def setUp(self):
        self.client = Client()
        self.current_year = int(settings.CURRENT_SEASON)
        cache.clear()  # Clear cache before each test
        
        # Create schools
        self.school1 = School.objects.create(name="Harvard University")
        self.school2 = School.objects.create(name="MIT")
        self.school3 = School.objects.create(name="Yale University")
        
        # Only one has recent debaters
        Debater.objects.create(
            first_name="Active",
            last_name="Student",
            school=self.school1,
            latest_season=str(self.current_year)
        )
    
    def test_all_schools_returned(self):
        """Test that all schools are returned regardless of activity"""
        response = self.client.get('/api/schools/all/')
        
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        
        self.assertEqual(data['count'], 3)
        self.assertEqual(len(data['schools']), 3)
    
    def test_schools_ordered_by_name(self):
        """Test that schools are ordered alphabetically"""
        response = self.client.get('/api/schools/all/')
        data = json.loads(response.content)
        
        schools = data['schools']
        self.assertEqual(schools[0]['name'], "Harvard University")
        self.assertEqual(schools[1]['name'], "MIT")
        self.assertEqual(schools[2]['name'], "Yale University")
    
    def test_schools_have_required_fields(self):
        """Test that each school has id and name"""
        response = self.client.get('/api/schools/all/')
        data = json.loads(response.content)
        
        for school in data['schools']:
            self.assertIn('id', school)
            self.assertIn('name', school)
    
    def test_response_is_cached(self):
        """Test that the response is cached for 5 minutes"""
        # First request - should hit database
        response1 = self.client.get('/api/schools/all/')
        data1 = json.loads(response1.content)
        
        # Create a new school
        School.objects.create(name="AAA New School")
        
        # Second request - should return cached data (not include new school)
        response2 = self.client.get('/api/schools/all/')
        data2 = json.loads(response2.content)
        
        # Data should be identical (cached)
        self.assertEqual(data1['count'], data2['count'])
        self.assertEqual(len(data1['schools']), len(data2['schools']))
        
        # Clear cache and try again
        cache.clear()
        response3 = self.client.get('/api/schools/all/')
        data3 = json.loads(response3.content)
        
        # Now should include the new school
        self.assertEqual(data3['count'], 4)
        school_names = [s['name'] for s in data3['schools']]
        self.assertIn("AAA New School", school_names)


class APISchoolDebatersViewTest(TestCase):
    """Test the school debaters API endpoint"""
    
    def setUp(self):
        self.client = Client()
        self.school = School.objects.create(name="Harvard University")
        
        # Get current year from settings
        self.current_year = int(settings.CURRENT_SEASON)
        
        # Create debaters with different activity periods
        # Active in current year
        self.debater1 = Debater.objects.create(
            first_name="Active",
            last_name="Current",
            school=self.school,
            status=Debater.VARSITY,
            first_season=str(self.current_year - 1),
            latest_season=str(self.current_year)
        )
        
        # Active 3 years ago (should be included)
        self.debater2 = Debater.objects.create(
            first_name="Recent",
            last_name="Graduate",
            school=self.school,
            status=Debater.NOVICE,
            first_season=str(self.current_year - 5),
            latest_season=str(self.current_year - 3)
        )
        
        # Active 6 years ago (should be excluded)
        self.debater3 = Debater.objects.create(
            first_name="Old",
            last_name="Graduate",
            school=self.school,
            status=Debater.VARSITY,
            first_season=str(self.current_year - 8),
            latest_season=str(self.current_year - 6)
        )
        
        # Exactly at 5 year boundary (should be included)
        self.debater4 = Debater.objects.create(
            first_name="Boundary",
            last_name="Case",
            school=self.school,
            status=Debater.VARSITY,
            first_season=str(self.current_year - 6),
            latest_season=str(self.current_year - 5)
        )
    
    def test_school_debaters_returns_active_debaters(self):
        """Test that endpoint returns only debaters active in last 5 years"""
        response = self.client.get(f'/api/debaters/{self.school.id}/')
        
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        
        # Should include debater1, debater2, and debater4, but not debater3
        self.assertEqual(data['count'], 3)
        self.assertEqual(len(data['debaters']), 3)
    
    def test_school_debaters_includes_school_info(self):
        """Test that response includes school information"""
        response = self.client.get(f'/api/debaters/{self.school.id}/')
        data = json.loads(response.content)
        
        self.assertIn('school', data)
        self.assertEqual(data['school']['id'], self.school.id)
        self.assertEqual(data['school']['name'], "Harvard University")
    
    def test_debater_fields_present(self):
        """Test that debaters have all required fields"""
        response = self.client.get(f'/api/debaters/{self.school.id}/')
        data = json.loads(response.content)
        
        for debater in data['debaters']:
            self.assertIn('id', debater)
            self.assertIn('name', debater)
            self.assertIn('first_name', debater)
            self.assertIn('last_name', debater)
            self.assertIn('status', debater)
            self.assertIn('school_id', debater)
            self.assertIn('school_name', debater)
    
    def test_debaters_ordered_by_name(self):
        """Test that debaters are ordered by last name then first name"""
        response = self.client.get(f'/api/debaters/{self.school.id}/')
        data = json.loads(response.content)
        
        debaters = data['debaters']
        # Should be: Boundary Case, Active Current, Recent Graduate
        self.assertEqual(debaters[0]['last_name'], "Case")
        self.assertEqual(debaters[1]['last_name'], "Current")
        self.assertEqual(debaters[2]['last_name'], "Graduate")
    
    def test_nonexistent_school_returns_404(self):
        """Test that requesting debaters for non-existent school returns 404"""
        response = self.client.get('/api/debaters/99999/')
        self.assertEqual(response.status_code, 404)
    
    def test_school_with_no_debaters(self):
        """Test school with no debaters returns empty list"""
        empty_school = School.objects.create(name="Empty School")
        response = self.client.get(f'/api/debaters/{empty_school.id}/')
        data = json.loads(response.content)
        
        self.assertEqual(data['count'], 0)
        self.assertEqual(len(data['debaters']), 0)
    
    def test_status_display(self):
        """Test that status is returned as human-readable text"""
        response = self.client.get(f'/api/debaters/{self.school.id}/')
        data = json.loads(response.content)
        
        # Find our varsity and novice debaters
        statuses = {d['first_name']: d['status'] for d in data['debaters']}
        self.assertEqual(statuses['Active'], 'Varsity')
        self.assertEqual(statuses['Recent'], 'Novice')
    
    def test_debaters_from_different_school_excluded(self):
        """Test that only debaters from requested school are returned"""
        other_school = School.objects.create(name="MIT")
        Debater.objects.create(
            first_name="Other",
            last_name="Student",
            school=other_school,
            latest_season=str(self.current_year)
        )
        
        response = self.client.get(f'/api/debaters/{self.school.id}/')
        data = json.loads(response.content)
        
        # Should only have Harvard debaters
        for debater in data['debaters']:
            self.assertEqual(debater['school_id'], self.school.id)
