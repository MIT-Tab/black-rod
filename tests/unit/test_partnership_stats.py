from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from core.utils.partnership_stats import _distance_stats, _interconnection_stats
from core.views.partnership_path_views import (
    STATS_CACHE_TIMEOUT,
    STATS_GENERATION_KEY,
    _stats_cache_key,
)


def _graph(edges):
    graph = {}
    for first, second in edges:
        graph.setdefault(first, set()).add(second)
        graph.setdefault(second, set()).add(first)
    return graph


def test_distances_include_all_diameter_ties_and_count_cumulative_reach():
    graph = _graph([
        (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8), (7, 9),
        (10, 11),
    ])
    graph[12] = set()
    diameter, pairs, reach = _distance_stats(graph)

    assert diameter == 7
    assert pairs == [(1, 8), (1, 9)]
    assert [layer.get(7, 0) for layer in reach] == [3, 4, 5, 6, 7, 8]
    assert [layer[1] for layer in reach] == [1, 2, 3, 4, 5, 6]
    assert [layer[10] for layer in reach] == [1] * 6
    assert all(12 not in layer for layer in reach)


def test_interconnection_counts_distinct_paths_without_revisiting_debaters():
    graph = _graph([(1, 3), (1, 4), (1, 5), (2, 3), (2, 4), (2, 5)])
    shared, three_hops, four_hops = _interconnection_stats(graph)

    assert shared == [(1, 2, 3), (3, 4, 2), (3, 5, 2), (4, 5, 2)]
    assert three_hops == [(1, 3, 2), (1, 4, 2), (1, 5, 2), (2, 3, 2), (2, 4, 2)]
    assert four_hops == [(3, 4, 2), (3, 5, 2), (4, 5, 2)]


def test_stats_load_separately_and_cache_the_fragment(client):
    key = _stats_cache_key()
    lock_key = f"{key}:lock"
    cache.delete_many([key, lock_key])
    try:
        with patch(
            "core.views.partnership_path_views.calculate_partnership_stats", return_value={}
        ) as calculate:
            page = client.get(reverse("core:partnership_stats"))
            assert page.status_code == 200
            assert b"Loading partnership statistics" in page.content
            calculate.assert_not_called()
            url = reverse("core:partnership_stats_results")
            cache.add(lock_key, True, timeout=120)
            assert client.get(url).status_code == 202
            calculate.assert_not_called()
            cache.delete(lock_key)

            with patch.object(cache, "set", wraps=cache.set) as store:
                results = client.get(url)
                assert results.status_code == 200
                assert b"Furthest apart" in results.content
                assert client.get(url).content == results.content
                calculate.assert_called_once_with()
                store.assert_called_once_with(
                    key, results.content.decode(), timeout=STATS_CACHE_TIMEOUT
                )
    finally:
        cache.delete_many([key, lock_key, STATS_GENERATION_KEY])


@pytest.mark.django_db
def test_only_superuser_can_clear_stats_cache_with_csrf(client, django_user_model):
    page_url = reverse("core:partnership_stats")
    clear_url = reverse("core:clear_partnership_stats_cache")
    results_url = reverse("core:partnership_stats_results")
    old_key = _stats_cache_key()
    cache.set(old_key, "Stale statistics")
    cache.set("other-cache-key", "Keep this")
    try:
        assert clear_url.encode() not in client.get(page_url).content
        assert client.post(clear_url).status_code == 403

        staff = django_user_model.objects.create_user(username="staff", is_staff=True)
        client.force_login(staff)
        assert clear_url.encode() not in client.get(page_url).content
        assert client.post(clear_url).status_code == 403
        assert cache.get(old_key) == "Stale statistics"

        admin = django_user_model.objects.create_superuser(username="admin")
        admin_client = Client(enforce_csrf_checks=True)
        admin_client.force_login(admin)
        assert clear_url.encode() in admin_client.get(page_url).content
        assert admin_client.get(clear_url).status_code == 405
        assert admin_client.post(clear_url).status_code == 403
        assert cache.get(old_key) == "Stale statistics"

        token = admin_client.cookies[settings.CSRF_COOKIE_NAME].value
        response = admin_client.post(clear_url, {"csrfmiddlewaretoken": token}, follow=True)
        assert response.redirect_chain == [(page_url, 302)]
        assert b"Partnership statistics cache cleared." in response.content
        assert cache.get(old_key) is None
        assert cache.get("other-cache-key") == "Keep this"

        # An in-flight request may finish writing under the retired cache key.
        cache.set(old_key, "Late stale statistics")
        with patch(
            "core.views.partnership_path_views.calculate_partnership_stats", return_value={}
        ) as calculate:
            results = client.get(results_url)
            assert b"Furthest apart" in results.content
            assert client.get(results_url).content == results.content
            calculate.assert_called_once_with()
    finally:
        cache.delete_many([old_key, _stats_cache_key(), STATS_GENERATION_KEY, "other-cache-key"])


@pytest.mark.django_db
def test_personal_stats_include_all_furthest_and_both_endpoint_orders(client):
    from core.models import Debater, DebaterAliasGroup, Team
    from core.utils.partnership_stats import calculate_debater_partnership_stats

    group = DebaterAliasGroup.objects.create()
    people = [
        Debater.objects.create(first_name=name)
        for name in ("Alex", "Blair", "Casey", "Drew", "Erin")
    ]
    people[0].alias_group = group
    people[0].save()
    alias = Debater.objects.create(first_name="Alex Alias", alias_group=group)
    for first, second in [(0, 1), (1, 2), (1, 3), (2, 4), (3, 4)]:
        team = Team.objects.create(name="Partners")
        team.debaters.add(alias if first == 0 else people[first], people[second])

    result = calculate_debater_partnership_stats(people[4])
    assert result["distance"] == 3
    assert result["furthest_debaters"] == [people[0]]
    assert result["personal_rankings"] == [
        [(people[1], 2)],
        [(people[0], 2), (people[2], 1), (people[3], 1)],
        [],
    ]
    alias_result = calculate_debater_partnership_stats(alias)
    assert alias_result["furthest_debaters"] == [people[4]]

    tied = calculate_debater_partnership_stats(people[2])
    assert tied["distance"] == 2
    assert tied["furthest_debaters"] == [people[0], people[3]]

    url = reverse("core:debater_partnership_stats")
    assert client.get(url).status_code == 200
    response = client.get(url, {"debater": people[4].pk})
    assert response.status_code == 200
    assert response.context["selected_debater"] == people[4]
    assert b"3 partnerships" in response.content
    assert b"Shared partners" in response.content
    assert b"3-hop paths" in response.content
    assert b"4-hop paths" in response.content
    assert client.get(url, {"debater": "invalid"}).context["form"].errors

    isolated = Debater.objects.create(first_name="Isolated")
    empty = calculate_debater_partnership_stats(isolated)
    assert empty["furthest_debaters"] == []
    assert empty["personal_rankings"] == [[], [], []]
