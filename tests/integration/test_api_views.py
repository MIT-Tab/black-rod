# pylint: disable=import-outside-toplevel
"""
Integration tests for API views.
"""

import json
from datetime import date
from django.test import TestCase, Client
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from core.models.school import School
from core.models.debater import Debater
from core.models.results.team import TeamResult
from core.models.team import Team
from core.models.tournament import Tournament


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
        
        # Schools should be ordered by most recent activity, with inactive schools last.
        harvard_debater = Debater.objects.create(
            first_name="Older",
            last_name="Student",
            school=self.school1,
            latest_season=str(self.current_year)
        )
        mit_debater = Debater.objects.create(
            first_name="Active",
            last_name="Student",
            school=self.school2,
            latest_season=str(self.current_year)
        )
        harvard_team = Team.objects.create(name="Harvard Team")
        harvard_team.debaters.add(harvard_debater)
        mit_team = Team.objects.create(name="MIT Team")
        mit_team.debaters.add(mit_debater)
        harvard_tournament = Tournament.objects.create(
            name="October Invitational",
            host=self.school1,
            date=date(self.current_year, 10, 1),
            season=str(self.current_year),
        )
        mit_tournament = Tournament.objects.create(
            name="November Invitational",
            host=self.school2,
            date=date(self.current_year, 11, 1),
            season=str(self.current_year),
        )
        TeamResult.objects.create(tournament=harvard_tournament, team=harvard_team, place=-1)
        TeamResult.objects.create(tournament=mit_tournament, team=mit_team, place=-1)
    
    def test_all_schools_returned(self):
        """Test that all schools are returned regardless of activity"""
        response = self.client.get('/api/schools/all/')
        
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        
        self.assertEqual(data['count'], 3)
        self.assertEqual(len(data['schools']), 3)
    
    def test_schools_ordered_by_recent_competition(self):
        """Test that schools are ordered by most recent debater activity"""
        response = self.client.get('/api/schools/all/')
        data = json.loads(response.content)
        
        schools = data['schools']
        self.assertEqual(schools[0]['name'], "MIT")
        self.assertEqual(schools[1]['name'], "Harvard University")
        self.assertEqual(schools[2]['name'], "Yale University")

    def test_schools_recent_competition_sort_is_not_n_plus_one(self):
        """Test that recency sorting does not add queries per school"""
        for i in range(10):
            school = School.objects.create(name=f"Extra School {i:02d}")
            debater = Debater.objects.create(
                first_name="Extra",
                last_name=f"Student-{i}",
                school=school,
                latest_season=str(self.current_year - (i % 3)),
            )
            team = Team.objects.create(name=f"Extra Team {i:02d}")
            team.debaters.add(debater)
            tournament = Tournament.objects.create(
                name=f"Extra Tournament {i:02d}",
                host=school,
                date=date(self.current_year - (i % 3), 9, i + 1),
                season=str(self.current_year - (i % 3)),
            )
            TeamResult.objects.create(tournament=tournament, team=team, place=-1)

        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get('/api/schools/all/')

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(ctx), 1)
    
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
        cache.clear()
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
            first_season=str(self.current_year - 1),
            latest_season=str(self.current_year)
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
        self._create_participation(self.debater1, date(self.current_year, 11, 1), "Current")
        self._create_participation(self.debater2, date(self.current_year, 9, 1), "Earlier")
        self._create_participation(
            self.debater4,
            date(self.current_year - 5, 10, 1),
            "Boundary",
        )

    def _create_participation(self, debater, tournament_date, suffix):
        team = Team.objects.create(name=f"{debater.name} {suffix}")
        team.debaters.add(debater)
        tournament = Tournament.objects.create(
            name=f"{suffix} Tournament",
            host=self.school,
            date=tournament_date,
            season=str(tournament_date.year),
        )
        TeamResult.objects.create(tournament=tournament, team=team, place=-1)
    
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
    
    def test_debaters_ordered_by_recent_competition(self):
        """Test that debaters are ordered by latest tournament date, then season and name"""
        response = self.client.get(f'/api/debaters/{self.school.id}/')
        data = json.loads(response.content)
        
        debaters = data['debaters']
        self.assertEqual(debaters[0]['last_name'], "Current")
        self.assertEqual(debaters[1]['last_name'], "Graduate")
        self.assertEqual(debaters[2]['last_name'], "Case")

    def test_debaters_recent_competition_sort_is_not_n_plus_one(self):
        """Test that recency sorting does not add queries per debater"""
        for i in range(10):
            debater = Debater.objects.create(
                first_name="Extra",
                last_name=f"Student-{i:02d}",
                school=self.school,
                latest_season=str(self.current_year - (i % 5)),
            )
            self._create_participation(
                debater,
                date(self.current_year - (i % 5), 8, i + 1),
                f"Extra {i:02d}",
            )

        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(f'/api/debaters/{self.school.id}/')

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(ctx), 2)
    
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


class APIScheduleViewTest(TestCase):
    """Test the public schedule API endpoint."""

    def setUp(self):
        self.client = Client()
        cache.clear()
        self.season = settings.CURRENT_SEASON
        year = int(self.season)

        self.school_host = School.objects.create(name="Alpha College")
        self.school_other = School.objects.create(name="Beta University")

        Tournament.objects.create(
            name="Alpha Invitational",
            manual_name="Alpha Invitational",
            host=self.school_host,
            date=date(year, 9, 6),
            season=self.season,
            toty=True,
            soty=True,
            qual=True,
            qual_type=Tournament.POINTS,
            autoqual_bar=4,
        )
        Tournament.objects.create(
            name="Expansion BP",
            manual_name="Expansion BP",
            host=self.school_host,
            date=date(year, 10, 5),
            season=self.season,
            toty=False,
            soty=False,
            qual=False,
            qual_type=Tournament.EXPANSION,
            autoqual_bar=2,
        )
        Tournament.objects.create(
            name="Nationals",
            manual_name="Nationals",
            host=self.school_other,
            date=date(year, 4, 1),
            season=self.season,
            toty=False,
            soty=False,
            qual=False,
            qual_type=Tournament.NATIONALS,
        )
        Tournament.objects.create(
            name="GM & BIPOC Invitational",
            manual_name="GM & BIPOC Invitational",
            host=self.school_other,
            date=date(year, 11, 15),
            season=self.season,
            toty=False,
            soty=False,
            qual=True,
            qual_type=Tournament.BIPOC,
        )

    def test_schedule_structure_matches_html(self):
        response = self.client.get('/api/schedule/')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)

        self.assertEqual(data['season'], self.season)
        self.assertIn('months', data)
        month_names = [block['display'] for block in data['months']]
        self.assertIn('September', month_names)

        september = next(block for block in data['months'] if block['display'] == 'September')
        week_block = next(week for week in september['weeks'] if week['date'] == 6)
        alpha_entry = next(item for item in week_block['tournaments'] if item['tournament']['name'] == "Alpha Invitational")
        self.assertTrue(alpha_entry['otys']['toty_points'])
        self.assertTrue(alpha_entry['otys']['soty_points'])
        self.assertTrue(alpha_entry['otys']['coty_points'])

        october = next(block for block in data['months'] if block['display'] == 'October')
        bp_entry = next(
            item for week in october['weeks']
            for item in week['tournaments']
            if item['tournament']['name'] == "Expansion BP"
        )
        self.assertFalse(bp_entry['otys']['toty_points'])
        self.assertIn('autoqual', ' '.join(bp_entry['otys']['notes']).lower())

        november = next(block for block in data['months'] if block['display'] == 'November')
        gm_entry = next(
            item for week in november['weeks']
            for item in week['tournaments']
            if "GM" in item['tournament']['name']
        )
        notes_text = ' '.join(note.lower() for note in gm_entry['otys']['notes'])
        self.assertIn('bipoc', notes_text)
        self.assertIn('autoqual', notes_text)

    def test_old_season_bipoc_no_autoqual_note(self):
        Tournament.objects.create(
            name="Legacy BIPOC Invitational",
            manual_name="Legacy BIPOC Invitational",
            host=self.school_other,
            date=date(2024, 11, 15),
            season="2024",
            toty=False,
            soty=False,
            qual=True,
            qual_type=Tournament.BIPOC,
        )

        response = self.client.get('/api/schedule/?season=2024')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        november = next(block for block in data['months'] if block['display'] == 'November')
        legacy_entry = next(
            item for week in november['weeks']
            for item in week['tournaments']
            if "Legacy BIPOC Invitational" in item['tournament']['name']
        )
        notes_text = ' '.join(note.lower() for note in legacy_entry['otys']['notes'])
        self.assertIn('bipoc', notes_text)
        self.assertNotIn('autoqual', notes_text)

    def test_custom_season(self):
        other_season = str(int(self.season) - 1)
        Tournament.objects.create(
            name="Old Season Event",
            manual_name="Old Season Event",
            host=self.school_host,
            date=date(int(other_season), 9, 1),
            season=other_season,
            toty=True,
            soty=True,
            qual=True,
        )

        response = self.client.get(f'/api/schedule/?season={other_season}')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)

        self.assertEqual(data['season'], other_season)
        month_names = [block['display'] for block in data['months']]
        self.assertEqual(month_names, ['September'])
        week = data['months'][0]['weeks'][0]
        names = [item['tournament']['name'] for item in week['tournaments']]
        self.assertIn("Old Season Event", names)
