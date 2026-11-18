import pytest
from django.urls import reverse

from core.models import Debater, DebaterAliasGroup, Team
from core.utils.partnership_paths import find_partnership_paths

pytestmark = pytest.mark.django_db


def _partner(*debaters, synthetic=False):
    team = Team.objects.create(name="Partnership", synthetic=synthetic)
    team.debaters.add(*debaters)


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


def test_partnership_paths_page_and_navigation_require_superuser(client, django_user_model):
    url = reverse("core:partnership_paths")
    assert client.get(url).status_code == 302

    user = django_user_model.objects.create_user(username="staff", is_staff=True)
    client.force_login(user)
    assert client.get(url).status_code == 403
    assert url.encode() not in client.get(reverse("core:team_list")).content

    user.is_superuser = True
    user.save(update_fields=["is_superuser"])
    first = Debater.objects.create(first_name="First Debater")
    last = Debater.objects.create(first_name="Last Debater")
    _partner(first, last)

    response = client.get(url, {"first_debater": first.pk, "second_debater": last.pk})
    assert response.status_code == 200
    assert response.context["paths"] == [[first, last]]
    assert url.encode() in response.content
    assert b"1 partnership" in response.content

    invalid = client.get(url, {"first_debater": first.pk, "second_debater": "invalid"})
    assert invalid.context["form"].errors
    assert "paths" not in invalid.context
