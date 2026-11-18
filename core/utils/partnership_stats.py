"""Statistics on the public debater partnership graph."""

from collections import Counter
from heapq import nlargest
from itertools import chain

from core.models import Debater
from core.utils.partnership_paths import build_partnership_graph


def _distance_layers(graph, source):
    """Yield debaters at each successive shortest distance from source."""
    frontier = {source}
    seen = frontier.copy()
    while frontier:
        frontier = set.union(set(), *(graph[vertex] for vertex in frontier)) - seen
        if frontier:
            seen.update(frontier)
            yield frontier


def _distance_stats(graph):
    diameter = 0
    furthest = []
    reach = [{} for _ in range(6)]
    for source in sorted(graph):
        for distance, layer in enumerate(_distance_layers(graph, source), start=1):
            if distance <= len(reach):
                reach[distance - 1][source] = len(layer)
            if distance >= diameter:
                if distance > diameter:
                    diameter = distance
                    furthest.clear()
                furthest.extend((source, end) for end in sorted(layer) if end > source)
    return diameter, furthest, reach


def _top_debaters(counts, limit):
    return nlargest(limit, counts.items(), key=lambda row: (row[1], -row[0]))


def _pair_path_counts(graph, source):
    """Count simple paths of lengths 2 through 4 from the smaller endpoint."""
    counts = [Counter() for _ in range(3)]
    visited = {source}

    def visit(vertex, distance):
        for neighbor in graph[vertex] - visited:
            if distance >= 2 and neighbor > source:
                counts[distance - 2][neighbor] += 1
            if distance < 4:
                visited.add(neighbor)
                visit(neighbor, distance + 1)
                visited.remove(neighbor)

    visit(source, 1)
    return counts


def _interconnection_stats(graph):
    leaders = [[], [], []]
    for source in graph:
        for index, counts in enumerate(_pair_path_counts(graph, source)):
            if counts:
                candidates = ((source, target, count) for target, count in counts.items())
                leaders[index] = nlargest(
                    5,
                    chain(leaders[index], candidates),
                    key=lambda row: (row[2], -row[0], -row[1]),
                )
    return leaders


def calculate_partnership_stats():
    _, graph = build_partnership_graph()
    diameter, furthest, reach = _distance_stats(graph)
    reach_leaders = [_top_debaters(counts, 3) for counts in reach]
    interconnection_leaders = _interconnection_stats(graph)
    ids = {vertex for pair in furthest for vertex in pair}
    ids.update(
        vertex
        for ranking in reach_leaders
        for vertex, _ in ranking
    )
    ids.update(
        vertex
        for ranking in interconnection_leaders
        for left, right, _ in ranking
        for vertex in (left, right)
    )
    debaters = Debater.objects.in_bulk(ids)

    return {
        "diameter": diameter,
        "furthest_pairs": [
            (debaters[left], debaters[right])
            for left, right in furthest
            if left in debaters and right in debaters
        ],
        "reach_leaders": [
            [(debaters[vertex], count) for vertex, count in ranking if vertex in debaters]
            for ranking in reach_leaders
        ],
        "interconnection_leaders": [
            [
                (debaters[left], debaters[right], count)
                for left, right, count in ranking
                if left in debaters and right in debaters
            ]
            for ranking in interconnection_leaders
        ],
    }
