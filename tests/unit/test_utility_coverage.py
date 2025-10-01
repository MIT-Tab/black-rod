"""Regression tests for Tournament model behaviour."""

from datetime import date, timedelta

import pytest

from core.models import School, Tournament


@pytest.mark.django_db
def test_points_tournaments_increment_suffix_for_same_host():
    host = School.objects.create(name="Suffix Host", included_in_oty=True)

    first = Tournament.objects.create(
        name="Initial",
        host=host,
        season="2024",
        date=date(2024, 1, 1),
        qual_type=Tournament.POINTS,
        num_teams=16,
    )
    first.save()

    second = Tournament.objects.create(
        name="Next",
        host=host,
        season="2024",
        date=date(2024, 2, 1),
        qual_type=Tournament.POINTS,
        num_teams=16,
    )
    second.save()

    assert first.name == host.name
    assert second.name == f"{host.name} II"


@pytest.mark.django_db
def test_manual_name_overrides_generated_name():
    host = School.objects.create(name="Manual Host", included_in_oty=True)
    tournament = Tournament.objects.create(
        name="Ignored",
        host=host,
        manual_name="Custom Name",
        season="2024",
        date=date(2024, 3, 1),
        qual_type=Tournament.POINTS,
        num_teams=16,
    )

    tournament.save()

    assert tournament.name == "Custom Name"


@pytest.mark.django_db
def test_special_qual_type_applies_suffix_and_flags():
    host = School.objects.create(name="Nationals Host", included_in_oty=True)
    tournament = Tournament.objects.create(
        name="Placeholder",
        host=host,
        season="2024",
        date=date(2024, 4, 1),
        qual_type=Tournament.NATIONALS,
        num_teams=16,
    )

    tournament.save()

    assert tournament.name.endswith(" Nationals")
    assert tournament.toty is False
    assert tournament.qual is False


@pytest.mark.django_db
def test_tournament_point_helpers_respect_flags():
    host = School.objects.create(name="Helpers Host", included_in_oty=True)
    tournament = Tournament.objects.create(
        name="Helpers",
        host=host,
        season="2024",
        date=date(2024, 5, 1),
        qual_type=Tournament.NATIONALS,
        num_teams=24,
        num_novice_debaters=20,
        online_qual_points=False,
    )

    tournament.toty = False
    tournament.soty = False
    tournament.noty = False

    assert tournament.get_toty_points(place=1) == 0
    assert tournament.get_soty_points(place=1) == 0
    assert tournament.get_noty_points(place=1) == 0
    assert tournament.get_online_qual_points(place=1) == 0


@pytest.mark.django_db
def test_get_season_display_formats_numeric_seasons():
    host = School.objects.create(name="Season Host", included_in_oty=True)
    tournament = Tournament.objects.create(
        name="Season",
        host=host,
        season="2024",
        date=date(2024, 6, 1),
        qual_type=Tournament.POINTS,
        num_teams=16,
    )

    assert tournament.get_season_display() == "2024-25"


@pytest.mark.django_db
def test_suffix_counts_only_previous_tournaments():
    host = School.objects.create(name="Suffix Counter", included_in_oty=True)
    earlier = Tournament.objects.create(
        name="Earlier",
        host=host,
        season="2024",
        date=date(2024, 1, 1),
        qual_type=Tournament.POINTS,
        num_teams=16,
    )
    earlier.save()

    simultaneous = Tournament.objects.create(
        name="Simultaneous",
        host=host,
        season="2024",
        date=date(2024, 1, 1) + timedelta(days=5),
        qual_type=Tournament.POINTS,
        num_teams=16,
    )
    simultaneous.save()

    later = Tournament.objects.create(
        name="Later",
        host=host,
        season="2024",
        date=date(2024, 1, 1) + timedelta(days=10),
        qual_type=Tournament.POINTS,
        num_teams=16,
    )
    later.save()

    assert earlier.name == host.name
    assert simultaneous.name == f"{host.name} II"
    assert later.name == f"{host.name} III"
