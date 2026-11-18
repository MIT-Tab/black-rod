from unittest.mock import patch

from django.core.cache import cache
from django.urls import reverse

from core.utils.partnership_stats import _distance_stats, _interconnection_stats
from core.views.partnership_path_views import STATS_CACHE_KEY, STATS_CACHE_TIMEOUT, STATS_LOCK_KEY


def _graph(edges):
    graph = {}
    for first, second in edges:
        graph.setdefault(first, set()).add(second)
        graph.setdefault(second, set()).add(first)
    return graph


def test_distances_include_all_diameter_ties_and_count_shortest_reach():
    graph = _graph([
        (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8), (7, 9),
        (10, 11),
    ])
    diameter, pairs, reach = _distance_stats(graph)

    assert diameter == 7
    assert pairs == [(1, 8), (1, 9)]
    assert [layer.get(7, 0) for layer in reach] == [3, 1, 1, 1, 1, 1]
    assert 10 not in reach[1]


def test_interconnection_counts_distinct_paths_without_revisiting_debaters():
    graph = _graph([(1, 3), (1, 4), (1, 5), (2, 3), (2, 4), (2, 5)])
    shared, three_hops, four_hops = _interconnection_stats(graph)

    assert shared == [(1, 2, 3), (3, 4, 2), (3, 5, 2), (4, 5, 2)]
    assert three_hops == [(1, 3, 2), (1, 4, 2), (1, 5, 2), (2, 3, 2), (2, 4, 2)]
    assert four_hops == [(3, 4, 2), (3, 5, 2), (4, 5, 2)]


def test_stats_load_separately_and_cache_the_fragment(client):
    cache.delete_many([STATS_CACHE_KEY, STATS_LOCK_KEY])
    try:
        with patch(
            "core.views.partnership_path_views.calculate_partnership_stats", return_value={}
        ) as calculate:
            page = client.get(reverse("core:partnership_stats"))
            assert page.status_code == 200
            assert b"Loading partnership statistics" in page.content
            calculate.assert_not_called()
            url = reverse("core:partnership_stats_results")
            cache.add(STATS_LOCK_KEY, True, timeout=120)
            assert client.get(url).status_code == 202
            calculate.assert_not_called()
            cache.delete(STATS_LOCK_KEY)

            with patch.object(cache, "set", wraps=cache.set) as store:
                results = client.get(url)
                assert results.status_code == 200
                assert b"Furthest apart" in results.content
                assert client.get(url).content == results.content
                calculate.assert_called_once_with()
                store.assert_called_once_with(
                    STATS_CACHE_KEY, results.content.decode(), timeout=STATS_CACHE_TIMEOUT
                )
    finally:
        cache.delete_many([STATS_CACHE_KEY, STATS_LOCK_KEY])
