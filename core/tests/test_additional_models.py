# pylint: disable=import-outside-toplevel
from datetime import date
import pytest

from core.models.team import Team
from core.models.debater import Debater
from core.models.school import School
from core.models.tournament import Tournament
from core.models.round import Round
from core.models.video import Video
from core.models.site_settings import SiteSetting


def test_simple_import():
    """Simple test to verify file is discovered"""
    assert True


def test_model_constants():
    """Test that model constants are properly defined"""
    assert hasattr(Tournament, "POINTS")
    assert hasattr(Tournament, "NATIONALS")
    assert hasattr(Video, "ALL")
    assert hasattr(Debater, "NOVICE")
    assert hasattr(Tournament, "POINTS")
    assert hasattr(Video, "ROUND_ONE")

@pytest.mark.django_db
def test_team_creation():
    """Test basic team creation"""
    school = School.objects.create(name="Test School")
    debater1 = Debater.objects.create(
        first_name="John", last_name="Doe", school=school
    )
    debater2 = Debater.objects.create(
        first_name="Jane", last_name="Smith", school=school
    )
    team = Team.objects.create(name="Test Team")
    team.debaters.add(debater1, debater2)
    assert team.debaters.count() == 2
    assert debater1 in team.debaters.all()
    assert debater2 in team.debaters.all()

@pytest.mark.django_db
def test_team_string_representation():
    """Test team string representation"""
    school = School.objects.create(name="Test School")
    debater1 = Debater.objects.create(
        first_name="John", last_name="Doe", school=school
    )
    debater2 = Debater.objects.create(
        first_name="Jane", last_name="Smith", school=school
    )
    team = Team.objects.create(name="Test Team")
    team.debaters.add(debater1, debater2)
    # Test that string representation doesn't crash
    str_repr = str(team)
    assert str_repr is not None

@pytest.mark.django_db
def test_team_with_single_debater():
    """Test team with only one debater"""
    school = School.objects.create(name="Test School")
    debater1 = Debater.objects.create(
        first_name="John", last_name="Doe", school=school
    )
    team = Team.objects.create(name="Single Debater Team")
    team.debaters.add(debater1)
    assert team.debaters.count() == 1
    assert debater1 in team.debaters.all()


@pytest.mark.django_db
class TestRoundModel:
    """Test Round model functionality"""

    def test_round_creation(self):
        """Test basic round creation"""
        # Create teams first
        gov_team = Team.objects.create(name="Team A")
        opp_team = Team.objects.create(name="Team B")
        tournament = Tournament.objects.create(
            name="Test Tournament",
            host=School.objects.create(name="Test School"),
            date=date.today(),
            season="2024",
        )

        round_obj = Round.objects.create(
            gov=gov_team, opp=opp_team, tournament=tournament, round_number=1
        )
        assert round_obj.gov == gov_team
        assert round_obj.opp == opp_team
        assert round_obj.tournament == tournament

    def test_round_string_representation(self):
        """Test round string representation"""
        # Create teams first
        gov_team = Team.objects.create(name="Team A")
        opp_team = Team.objects.create(name="Team B")
        tournament = Tournament.objects.create(
            name="Test Tournament",
            host=School.objects.create(name="Test School 2"),
            date=date.today(),
            season="2024",
        )

        round_obj = Round.objects.create(
            gov=gov_team, opp=opp_team, tournament=tournament, round_number=1
        )
        # Just test that string representation doesn't crash
        str_repr = str(round_obj)
        assert str_repr is not None


@pytest.mark.django_db
class TestVideoModel:
    """Test Video model functionality"""

    def test_video_creation(self):
        """Test basic video creation"""
        # Create required objects
        school = School.objects.create(name="Video School")
        tournament = Tournament.objects.create(
            name="Video Tournament", host=school, date=date.today(), season="2024"
        )
        pm = Debater.objects.create(first_name="PM", last_name="Debater", school=school)
        lo = Debater.objects.create(first_name="LO", last_name="Debater", school=school)
        mg = Debater.objects.create(first_name="MG", last_name="Debater", school=school)
        mo = Debater.objects.create(first_name="MO", last_name="Debater", school=school)

        video = Video.objects.create(
            pm=pm, lo=lo, mg=mg, mo=mo, tournament=tournament, round=Video.ROUND_ONE
        )
        assert video.pm == pm
        assert video.lo == lo

    def test_video_string_representation(self):
        """Test video string representation"""
        # Create required objects
        school = School.objects.create(name="Video School 2")
        tournament = Tournament.objects.create(
            name="Video Tournament 2", host=school, date=date.today(), season="2024"
        )
        pm = Debater.objects.create(first_name="PM", last_name="Debater", school=school)
        lo = Debater.objects.create(first_name="LO", last_name="Debater", school=school)
        mg = Debater.objects.create(first_name="MG", last_name="Debater", school=school)
        mo = Debater.objects.create(first_name="MO", last_name="Debater", school=school)

        video = Video.objects.create(
            pm=pm, lo=lo, mg=mg, mo=mo, tournament=tournament, round=Video.ROUND_ONE
        )
        # Just test that string representation doesn't crash
        str_repr = str(video)
        assert str_repr is not None


@pytest.mark.django_db
class TestSiteSettingModel:
    """Test SiteSetting model functionality"""

    def test_site_setting_creation(self):
        """Test basic site setting creation"""
        setting = SiteSetting.objects.create(key="test_key", value="test_value")
        assert setting.key == "test_key"
        assert setting.value == "test_value"

    def test_site_setting_get_setting_method(self):
        """Test get_setting class method"""
        SiteSetting.objects.create(key="test_setting", value="test_value")
        result = SiteSetting.get_setting("test_setting", "default")
        assert result == "test_value"

        # Test default value when setting doesn't exist
        result = SiteSetting.get_setting("nonexistent", "default_value")
        assert result == "default_value"

    def test_site_setting_set_setting_method(self):
        """Test set_setting class method"""
        SiteSetting.set_setting("new_setting", "new_value")
        setting = SiteSetting.objects.get(key="new_setting")
        assert setting.value == "new_value"

        # Test updating existing setting
        SiteSetting.set_setting("new_setting", "updated_value")
        setting = SiteSetting.objects.get(key="new_setting")
        assert setting.value == "updated_value"

    def test_site_setting_string_representation(self):
        """Test site setting string representation"""
        setting = SiteSetting.objects.create(key="test_key", value="test_value")
        assert str(setting) == "test_key: test_value"


@pytest.mark.django_db
class TestModelEdgeCases:
    """Test edge cases and special behaviors"""

    def test_debater_name_with_empty_strings(self):
        """Test debater creation with empty names"""
        school = School.objects.create(name="Test School")
        debater = Debater.objects.create(first_name="", last_name="", school=school)
        assert debater.first_name == ""
        assert debater.last_name == ""

    def test_debater_save_method(self):
        """Test debater save method"""
        school = School.objects.create(name="Test School")
        debater = Debater(first_name="John", last_name="Doe", school=school)
        debater.save()
        assert debater.pk is not None

    def test_tournament_different_qual_types(self):
        """Test tournament with different qualification types"""
        school = School.objects.create(name="Host School")
        tournament = Tournament(
            name="Test Tournament",
            date=date.today(),
            host=school,
            season="2024",
            qual_type=Tournament.POINTS,
        )
        tournament.save()
        assert tournament.qual_type == Tournament.POINTS

    def test_tournament_season_display(self):
        """Test tournament season display"""
        school = School.objects.create(name="Host School")
        tournament = Tournament(
            name="Test Tournament", date=date.today(), host=school, season="2024"
        )
        tournament.save()
        assert tournament.season == "2024"


def test_tournament_qual_types():
    """Test tournament qualification type constants"""
    assert Tournament.POINTS == 0
    assert Tournament.NATIONALS == 8


@pytest.mark.django_db
def test_model_field_validation():
    """Test basic model field validation"""
    # Test school creation
    school = School.objects.create(name="Test School")
    assert school.name == "Test School"

    # Test debater creation
    debater = Debater.objects.create(first_name="John", last_name="Doe", school=school)
    assert debater.first_name == "John"
    assert debater.last_name == "Doe"
    assert debater.school == school
