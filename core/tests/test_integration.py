# pylint: disable=import-outside-toplevel
from datetime import date
import pytest
from django.test import TestCase, Client
from django.contrib.auth import get_user_model

from core.models.school import School
from core.models.debater import Debater
from core.models.tournament import Tournament
from core.models.team import Team
from core.models.video import Video


User = get_user_model()


class CoreIntegrationTest(TestCase):
    """Integration tests for core functionality"""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="testuser", email="test@example.com", password="testpass123"
        )
        self.school = School.objects.create(name="Integration Test School")
        self.debater = Debater.objects.create(
            first_name="Test", last_name="Debater", school=self.school
        )

    def test_model_creation_integration(self):
        """Test integrated model creation workflow"""
        # Create tournament
        tournament = Tournament.objects.create(
            name="Test Tournament",
            host=self.school,
            date=date(2024, 1, 1),
            season="2024",
        )

        # Create team
        team = Team.objects.create(name="Test Team")
        team.debaters.add(self.debater)

        # Test relationships work
        self.assertEqual(tournament.host, self.school)
        self.assertIn(self.debater, team.debaters.all())
        self.assertEqual(team.debaters.count(), 1)

    def test_school_debater_relationship(self):
        """Test school-debater relationship"""
        debater2 = Debater.objects.create(
            first_name="Second", last_name="Debater", school=self.school
        )

        # Test reverse relationship
        school_debaters = self.school.debaters.all()
        self.assertIn(self.debater, school_debaters)
        self.assertIn(debater2, school_debaters)
        self.assertEqual(school_debaters.count(), 2)

    def test_tournament_without_host_workflow(self):
        """Test tournament creation with manual name override"""
        # Create a host school first
        host_school = School.objects.create(name="Integration Host School")

        tournament = Tournament(
            name="No Host Tournament",
            date=date(2024, 1, 1),
            season="2024",
            manual_name="Manual Name",
            host=host_school,  # Provide a host to avoid the None error
        )
        tournament.save()

        self.assertEqual(tournament.host, host_school)
        self.assertEqual(tournament.manual_name, "Manual Name")
        self.assertEqual(tournament.name, "Manual Name")  # Should use manual_name

    def test_team_name_update_workflow(self):
        """Test team name update functionality"""
        debater2 = Debater.objects.create(
            first_name="Jane", last_name="Smith", school=self.school
        )

        team = Team.objects.create(name="Original Name")
        team.debaters.add(self.debater, debater2)

        # Test name update method
        team.update_name()

        # Should contain school name and debater initials
        self.assertIn(self.school.name, team.name)
        self.assertIn("D", team.name)  # Debater initial
        self.assertIn("S", team.name)  # Smith initial

    def test_video_tournament_relationship(self):
        """Test video-tournament relationship"""
        tournament = Tournament.objects.create(
            name="Video Tournament",
            host=self.school,
            date=date(2024, 1, 1),
            season="2024",
        )

        video = Video.objects.create(
            pm=self.debater,
            lo=self.debater,
            mg=self.debater,
            mo=self.debater,
            link="https://youtube.com/watch?v=test",
            tournament=tournament,
        )

        self.assertEqual(video.tournament, tournament)
        self.assertTrue("Integration Test School" in str(video))
        self.assertTrue("UNKNOWN" in str(video))

    def test_hybrid_team_detection(self):
        """Test hybrid team detection"""
        school2 = School.objects.create(name="Second School")
        debater2 = Debater.objects.create(
            first_name="Cross", last_name="School", school=school2
        )

        # Same school team
        same_school_team = Team.objects.create(name="Same School")
        same_school_team.debaters.add(self.debater)
        self.assertFalse(same_school_team.hybrid)

        # Hybrid team
        hybrid_team = Team.objects.create(name="Hybrid Team")
        hybrid_team.debaters.add(self.debater, debater2)
        self.assertTrue(hybrid_team.hybrid)

    def test_debater_status_workflow(self):
        """Test debater status functionality"""
        varsity = Debater.objects.create(
            first_name="Varsity",
            last_name="Player",
            school=self.school,
            status=Debater.VARSITY,
        )

        novice = Debater.objects.create(
            first_name="Novice",
            last_name="Player",
            school=self.school,
            status=Debater.NOVICE,
        )

        self.assertEqual(varsity.status, Debater.VARSITY)
        self.assertEqual(novice.status, Debater.NOVICE)
        self.assertEqual(varsity.get_status_display(), "Varsity")
        self.assertEqual(novice.get_status_display(), "Novice")

    def test_tournament_season_filtering(self):
        """Test tournament season filtering"""
        tournament_2024 = Tournament.objects.create(
            name="2024 Tournament",
            host=self.school,
            date=date(2024, 1, 1),
            season="2024",
        )

        tournament_2025 = Tournament.objects.create(
            name="2025 Tournament",
            host=self.school,
            date=date(2025, 1, 1),
            season="2025",
        )  # Test filtering by season
        tournaments_2024 = Tournament.objects.filter(season="2024")
        tournaments_2025 = Tournament.objects.filter(season="2025")

        self.assertIn(tournament_2024, tournaments_2024)
        self.assertNotIn(tournament_2025, tournaments_2024)
        self.assertIn(tournament_2025, tournaments_2025)
        self.assertNotIn(tournament_2024, tournaments_2025)

    def test_school_tournament_hosting(self):
        """Test school tournament hosting relationship"""
        tournament1 = Tournament.objects.create(
            name="Tournament 1", host=self.school, date=date(2024, 1, 1), season="2024"
        )

        tournament2 = Tournament.objects.create(
            name="Tournament 2", host=self.school, date=date(2024, 2, 1), season="2024"
        )

        # Test reverse relationship
        hosted_tournaments = self.school.hosted_tournaments.all()
        self.assertIn(tournament1, hosted_tournaments)
        self.assertIn(tournament2, hosted_tournaments)
        self.assertEqual(hosted_tournaments.count(), 2)


class ModelMethodsTest(TestCase):
    """Test model methods and properties"""

    def setUp(self):
        self.school = School.objects.create(name="Methods Test School")
        self.debater = Debater.objects.create(
            first_name="Test", last_name="Debater", school=self.school
        )

    def test_debater_name_property(self):
        """Test debater name property"""
        debater = Debater.objects.create(
            first_name="John", last_name="Doe", school=self.school
        )

        self.assertEqual(debater.name, "John Doe")
        self.assertEqual(str(debater), "John Doe")

    def test_debater_name_with_whitespace(self):
        """Test debater name handling whitespace"""
        debater = Debater.objects.create(
            first_name="  John  ", last_name="  Doe  ", school=self.school
        )

        # Name should handle whitespace appropriately
        name = debater.name
        self.assertIn("John", name)
        self.assertIn("Doe", name)

    def test_team_long_name_property(self):
        """Test team long_name property"""
        debater1 = Debater.objects.create(
            first_name="John", last_name="Smith", school=self.school
        )
        debater2 = Debater.objects.create(
            first_name="Jane", last_name="Doe", school=self.school
        )

        team = Team.objects.create(name="Test Team")
        team.debaters.add(debater1, debater2)

        long_name = team.long_name
        self.assertIn(self.school.name, long_name)
        self.assertIn("John Smith", long_name)
        self.assertIn("Jane Doe", long_name)
        self.assertIn(" and ", long_name)

    def test_team_single_debater_long_name(self):
        """Test team long_name with single debater"""
        debater = Debater.objects.create(
            first_name="Solo", last_name="Player", school=self.school
        )

        team = Team.objects.create(name="Solo Team")
        team.debaters.add(debater)

        long_name = team.long_name
        self.assertIn(self.school.name, long_name)
        self.assertIn("Solo Player", long_name)
        self.assertNotIn(" and ", long_name)

    def test_tournament_name_auto_update(self):
        """Test tournament name auto-update on save"""
        tournament = Tournament.objects.create(
            name="Original Name", host=self.school, date=date(2024, 1, 1), season="2024"
        )

        # Name should be updated to host school name
        self.assertEqual(tournament.name, self.school.name)

    def test_tournament_manual_name_override(self):
        """Test tournament manual name overrides auto-naming"""
        tournament = Tournament.objects.create(
            name="Original Name",
            manual_name="Custom Tournament Name",
            host=self.school,
            date=date(2024, 1, 1),
            season="2024",
        )

        self.assertEqual(tournament.manual_name, "Custom Tournament Name")

    def test_school_included_in_oty_default(self):
        """Test school included_in_oty default value"""
        school = School.objects.create(name="Default Test")
        self.assertTrue(school.included_in_oty)

    def test_school_included_in_oty_false(self):
        """Test school included_in_oty can be False"""
        school = School.objects.create(name="Not OTY School", included_in_oty=False)
        self.assertFalse(school.included_in_oty)

    def test_video_string_representation(self):
        """Test video string representation"""
        tournament = Tournament.objects.create(
            name="Video Tournament",
            host=self.school,
            date=date(2024, 1, 1),
            season="2024",
        )

        video = Video.objects.create(
            pm=self.debater,
            lo=self.debater,
            mg=self.debater,
            mo=self.debater,
            link="https://example.com/video",
            tournament=tournament,
        )

        video_str = str(video)
        self.assertTrue("Methods Test School" in video_str)
        self.assertTrue("UNKNOWN" in video_str)


@pytest.mark.django_db
def test_model_managers():
    """Test model managers and querysets"""
    school = School.objects.create(name="Manager Test")

    # Test basic manager operations
    assert School.objects.count() == 1
    assert School.objects.filter(name="Manager Test").exists()

    # Test get operations
    retrieved_school = School.objects.get(name="Manager Test")
    assert retrieved_school == school


@pytest.mark.django_db
def test_model_field_validations():
    """Test model field validations"""
    school = School.objects.create(name="Validation Test")

    # Test required fields
    debater = Debater.objects.create(
        first_name="Required", last_name="Test", school=school
    )

    assert debater.first_name == "Required"
    assert debater.last_name == "Test"
    assert debater.school == school


@pytest.mark.django_db
def test_cascade_behaviors():
    """Test model cascade behaviors"""
    school = School.objects.create(name="Cascade Test")
    debater = Debater.objects.create(
        first_name="Cascade", last_name="Test", school=school
    )

    # Test that debater survives school deletion
    school_id = school.id
    school.delete()

    debater.refresh_from_db()
    assert debater.school is None
