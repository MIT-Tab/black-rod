"""Find short, loopless paths through the debater partnership graph."""

from collections import defaultdict, deque
from heapq import heappop, heappush
from itertools import combinations, groupby
from operator import itemgetter

from core.models import Debater, Team, TeamResult
from core.utils.debater_aliases import build_representative_debater_maps


def _shortest_path(graph, start, end, blocked_vertices, blocked_edges):
    parents = {start: None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if current == end:
            path = []
            while current is not None:
                path.append(current)
                current = parents[current]
            return tuple(reversed(path))

        for neighbor in graph.get(current, ()):
            if neighbor in parents or neighbor in blocked_vertices:
                continue
            if (current, neighbor) in blocked_edges:
                continue
            parents[neighbor] = current
            queue.append(neighbor)
    return None


def _shortest_paths(graph, start, end, limit):
    """Use Yen's algorithm to find alternatives to each accepted path."""
    first = _shortest_path(graph, start, end, set(), set())
    if first is None:
        return []

    paths = [first]
    candidates = []
    queued = set()
    while len(paths) < limit:
        previous = paths[-1]
        for index, vertex in enumerate(previous[:-1]):
            prefix = previous[:index + 1]
            blocked_edges = {
                (vertex, path[index + 1])
                for path in paths
                if len(path) > index + 1 and path[:index + 1] == prefix
            }
            suffix = _shortest_path(graph, vertex, end, set(prefix[:-1]), blocked_edges)
            if suffix is not None:
                candidate = prefix[:-1] + suffix
                if candidate not in queued:
                    heappush(candidates, (len(candidate), candidate))
                    queued.add(candidate)

        if not candidates:
            break
        paths.append(heappop(candidates)[1])
    return paths


def find_partnership_paths(start, end):
    public_debaters = Debater.objects.all()
    representatives, _ = build_representative_debater_maps(
        public_debaters.values_list("id", "alias_group_id")
    )
    start_id = representatives[start.pk]
    end_id = representatives[end.pk]
    if start_id == end_id:
        return [[start]]

    neighbors = defaultdict(set)
    redacted_teams = TeamResult.objects.filter(
        hidden_from_debaters__isnull=False,
    ).values("team_id")
    memberships = (
        Team.debaters.through.objects.filter(team__synthetic=False, debater__in=public_debaters)
        .exclude(team_id__in=redacted_teams)
        .order_by("team_id")
        .values_list("team_id", "debater_id")
    )
    for _, team_members in groupby(memberships.iterator(), key=itemgetter(0)):
        member_ids = {representatives[debater_id] for _, debater_id in team_members}
        for left, right in combinations(member_ids, 2):
            neighbors[left].add(right)
            neighbors[right].add(left)

    graph = {vertex: sorted(adjacent) for vertex, adjacent in neighbors.items()}
    paths = _shortest_paths(graph, start_id, end_id, limit=5)
    intermediate_ids = {vertex for path in paths for vertex in path[1:-1]}
    debaters = public_debaters.in_bulk(intermediate_ids)
    debaters.update({start_id: start, end_id: end})
    return [[debaters[vertex] for vertex in path] for path in paths]
