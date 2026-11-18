from datetime import date

import pytest
from django.urls import reverse

from core.models import Debater, DebaterAliasGroup, School, Team, TeamResult, Tournament
from core.utils.partnership_paths import find_partnership_paths

pytestmark = pytest.mark.django_db


def _partner(*debaters, synthetic=False):
    team = Team.objects.create(name="Partnership", synthetic=synthetic)
    team.debaters.add(*debaters)
    return team


def test_paths_are_ranked_and_deduplicated_across_aliases():
    group = DebaterAliasGroup.objects.create()
    first = Debater.objects.create(first_name="First", alias_group=group)
    alias = Debater.objects.create(first_name="Also First", alias_group=group)
    last = Debater.objects.create(first_name="Last")
    _partner(first, alias)
    _partner(first, last)
    _partner(alias, last)
    for index in range(6):
        middle = Debater.objects.create(first_name=f"Partner {index}")
        _partner(alias, middle)
        _partner(middle, last)

    paths = find_partnership_paths(first, last)
    assert [len(path) for path in paths] == [2, 3, 3, 3, 3]
    assert paths[0] == [first, last]
    assert all(path[0] == first and path[-1] == last for path in paths)
    assert len({path[1] for path in paths[1:]}) == 4
    assert find_partnership_paths(first, alias) == [[first]]


def test_paths_can_share_vertices_and_include_longer_alternatives():
    first, left, right, last = [
        Debater.objects.create(first_name=name)
        for name in ("First", "Left", "Right", "Last")
    ]
    for partnership in ((first, left), (left, last), (first, right), (right, last), (left, right)):
        _partner(*partnership)

    assert find_partnership_paths(first, last) == [
        [first, left, last],
        [first, right, last],
        [first, left, right, last],
        [first, right, left, last],
    ]


def test_synthetic_teams_and_profiles_do_not_connect_debaters():
    first = Debater.objects.create(first_name="First")
    last = Debater.objects.create(first_name="Last")
    synthetic = Debater.all_objects.create(first_name="Placeholder", synthetic=True)
    _partner(first, last, synthetic=True)
    _partner(first, synthetic)
    _partner(synthetic, last)

    assert find_partnership_paths(first, last) == []


def test_redacted_partnership_and_temporary_profile_are_excluded():
    first = Debater.objects.create(first_name="First")
    last = Debater.objects.create(first_name="Last")
    temporary = Debater.all_objects.create(first_name="Temporary", temporary=True)
    _partner(first, temporary)
    _partner(temporary, last)
    team = _partner(first, last)
    school = School.objects.create(name="Host")
    tournament = Tournament.objects.create(host=school, season="2026", date=date(2026, 1, 1))
    result = TeamResult.objects.create(team=team, tournament=tournament)
    result.hidden_from_debaters.add(first)

    assert find_partnership_paths(first, last) == []


def test_partnership_paths_page_and_fun_menu_are_public(client):
    url = reverse("core:partnership_paths")
    first = Debater.objects.create(first_name="First Debater")
    last = Debater.objects.create(first_name="Last Debater")
    temporary = Debater.all_objects.create(first_name="Temporary", temporary=True)
    _partner(first, last)

    response = client.get(url, {"first_debater": first.pk, "second_debater": last.pk})
    assert response.status_code == 200
    assert response.context["paths"] == [[first, last]]
    assert b"Fun" in response.content
    assert f'href="{url}">Debater paths'.encode() in response.content
    assert b"1 partnership" in response.content

    invalid = client.get(url, {"first_debater": first.pk, "second_debater": temporary.pk})
    assert invalid.context["form"].errors
    assert "paths" not in invalid.context


def test_partnership_autocomplete_prioritizes_exact_full_name(client):
    exact = Debater.objects.create(first_name="Alex", last_name="Lee")
    prefix = Debater.objects.create(first_name="Alex", last_name="Leeman")
    reversed_name = Debater.objects.create(first_name="Lee", last_name="Alex")
    Debater.objects.create(first_name="Alex", last_name="Kim")
    Debater.objects.create(first_name="Terry", last_name="Lee")
    Debater.all_objects.create(first_name="Alex", last_name="Lee", temporary=True)
    Debater.all_objects.create(first_name="Alex", last_name="Lee", synthetic=True)

    response = client.get(reverse("core:partnership_debater_autocomplete"), {"q": "  aLEX   lee "})

    assert response.status_code == 200
    assert [row["id"] for row in response.json()["results"]] == [
        str(exact.pk), str(prefix.pk), str(reversed_name.pk)
    ]
