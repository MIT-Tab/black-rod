"""Public partnership network, weighted by distinct tournaments together."""

from collections import defaultdict
from itertools import combinations

from core.models import Debater, QUAL, QualPoints, Round, SpeakerResult, Team, TeamResult, TOTY
from core.utils.debater_aliases import build_representative_debater_maps
from core.utils.partnership_paths import _shortest_paths
from core.utils.elo_runtime_engine.constants import season_to_int


def _season_filter(queryset, filters, prefix="tournament__"):
    if filters.get("from_season"):
        queryset = queryset.filter(**{f"{prefix}season__gte": filters["from_season"]})
    if filters.get("to_season"):
        queryset = queryset.filter(**{f"{prefix}season__lte": filters["to_season"]})
    return queryset



def _axis_totals(filters, representatives, memberships):
    toty_points = defaultdict(float)
    qual_points = defaultdict(float)
    qualifications = defaultdict(set)
    for team_id, points in _season_filter(
        TOTY.objects.filter(team_id__in=memberships, points__gte=0), filters, prefix=""
    ).values_list("team_id", "points"):
        for person_id in memberships[team_id]:
            toty_points[person_id] += points
    for person_id, points in _season_filter(
        QualPoints.objects.filter(debater_id__in=representatives), filters, prefix=""
    ).values_list("debater_id", "points"):
        qual_points[representatives[person_id]] += points
    for person_id, season in _season_filter(
        QUAL.objects.filter(debater_id__in=representatives), filters, prefix=""
    ).values_list("debater_id", "season"):
        qualifications[representatives[person_id]].add(season)
    return toty_points, qual_points, qualifications


def build_partnership_network(filters):
    people = list(Debater.objects.select_related("school").order_by("pk"))
    representatives, groups = build_representative_debater_maps(
        (person.pk, person.alias_group_id) for person in people
    )
    people_by_id = {person.pk: person for person in people}
    hidden_teams = TeamResult.objects.filter(
        hidden_from_debaters__isnull=False
    ).values("team_id")
    memberships = defaultdict(set)
    for team_id, person_id in (
        Team.debaters.through.objects.filter(
            team__synthetic=False, debater__in=Debater.objects.all()
        ).exclude(team_id__in=hidden_teams).values_list("team_id", "debater_id")
    ):
        memberships[team_id].add(representatives[person_id])

    team_tournaments = defaultdict(set)
    results = _season_filter(TeamResult.objects.filter(team_id__in=memberships), filters)
    rounds = _season_filter(Round.objects.all(), filters)
    for rows in (
        results.values_list("team_id", "tournament_id"),
        rounds.filter(gov_id__in=memberships).values_list("gov_id", "tournament_id"),
        rounds.filter(opp_id__in=memberships).values_list("opp_id", "tournament_id"),
    ):
        for team_id, tournament_id in rows.iterator():
            team_tournaments[team_id].add(tournament_id)

    tournaments = defaultdict(set)
    partnerships = defaultdict(set)
    season_limited = bool(filters.get("from_season") or filters.get("to_season"))
    for team_id, members in memberships.items():
        events = team_tournaments[team_id]
        if season_limited and not events:
            continue
        for person_id in members:
            tournaments[person_id].update(events)
        for pair in combinations(sorted(members), 2):
            partnerships[pair].update(events)

    speaker_results = _season_filter(
        SpeakerResult.objects.filter(
            debater_id__in=representatives, hidden_from_profile=False
        ), filters
    )
    for person_id, tournament_id in speaker_results.values_list("debater_id", "tournament_id"):
        tournaments[representatives[person_id]].add(tournament_id)

    toty_points, qual_points, qualifications = _axis_totals(filters, representatives, memberships)
    qualified = set(qualifications)
    selected_schools = {school.pk for school in filters.get("schools", [])}
    eligible = set()
    for person_id, member_ids in groups.items():
        school_ids = {people_by_id[member].school_id for member in member_ids}
        if selected_schools and not selected_schools.intersection(school_ids):
            continue
        if filters.get("qualified") and person_id not in qualified:
            continue
        if len(tournaments[person_id]) < (filters.get("min_tournaments") or 0):
            continue
        eligible.add(person_id)

    edges = [
        {"from": left, "to": right, "tournaments": len(events)}
        for (left, right), events in sorted(partnerships.items())
        if left in eligible and right in eligible
    ]
    connected = {person_id for edge in edges for person_id in (edge["from"], edge["to"])}
    partners = defaultdict(set)
    for edge in edges:
        partners[edge["from"]].add(edge["to"])
        partners[edge["to"]].add(edge["from"])
    nodes = []
    for person_id in sorted(connected):
        person = people_by_id[person_id]
        schools = sorted({
            people_by_id[member].school.name
            for member in groups[person_id] if people_by_id[member].school
        })
        first_years = [
            season_to_int(people_by_id[member].first_season) for member in groups[person_id]
        ]
        final_years = [
            season_to_int(people_by_id[member].latest_season) for member in groups[person_id]
        ]
        nodes.append({
            "id": person_id,
            "label": person.name,
            "schools": schools,
            "school_id": person.school_id or 0,
            "tournaments": len(tournaments[person_id]),
            "qualified": person_id in qualified,
            "url": person.get_absolute_url(),
            "toty_points": toty_points[person_id],
            "qual_points": qual_points[person_id],
            "qualification_count": len(qualifications[person_id]),
            "partner_count": len(partners[person_id]),
            "first_year": min((year for year in first_years if year is not None), default=None),
            "final_year": max((year for year in final_years if year is not None), default=None),
        })
    return {"nodes": nodes, "edges": edges}


def find_network_paths(filters, first_debater, second_debater):
    data = build_partnership_network(filters)
    nodes = {node["id"]: node for node in data["nodes"]}
    graph = {person_id: set() for person_id in nodes}
    for edge in data["edges"]:
        graph[edge["from"]].add(edge["to"])
        graph[edge["to"]].add(edge["from"])
    representatives, _ = build_representative_debater_maps(
        Debater.objects.values_list("id", "alias_group_id")
    )
    first = representatives[first_debater.pk]
    second = representatives[second_debater.pk]
    if first not in graph or second not in graph:
        return []
    graph = {person_id: sorted(neighbors) for person_id, neighbors in graph.items()}
    return [
        [nodes[person_id] for person_id in path]
        for path in _shortest_paths(graph, first, second, limit=5)
    ]
