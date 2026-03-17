import concurrent.futures
import copy

import networkx as nx

from .config import get_rank_penalties, get_region_penalties, merge_scheduler_settings
from .loader import (
    SchedulerDataError,
    count_scheduler_scenarios,
    load_scheduler_inputs,
)


class SchedulerExecutionError(RuntimeError):
    pass


def _get_penalty(school, date, settings):
    penalty = 10000
    details = {}
    rank_penalties = get_rank_penalties(settings)
    region_penalties = get_region_penalties(settings)

    preference_value = school.availability.get(date.date) or "Impossible"
    if preference_value not in rank_penalties:
        raise SchedulerDataError(
            f"{school.name} has an unsupported preference value for {date.date}: {preference_value}."
        )

    priority = school.priority or 1
    rank_penalty = rank_penalties[preference_value] * priority
    penalty += rank_penalty
    if preference_value != "Already Scheduled":
        details["preference"] = rank_penalty

    if (
        school.region == "Central"
        and date.region in {"North", "South"}
        and date.active_tournament_count == 2
    ):
        region_penalty = settings["central_on_two_tournament_weekend_penalty"]
        penalty += region_penalty
        details["region"] = region_penalty
    elif (school.region, date.region) in region_penalties:
        region_penalty = region_penalties[(school.region, date.region)]
        penalty += region_penalty
        details["region"] = region_penalty

    if date.tags:
        missing_tags = [tag for tag in date.tags if tag not in school.tags]
        if missing_tags:
            penalty += settings["missing_tag_penalty"]
            details[f"missing tags: {', '.join(missing_tags)}"] = settings[
                "missing_tag_penalty"
            ]
        else:
            penalty += settings["tag_bonus"]

    if date.unopposed and "Unopposed" not in school.tags:
        penalty += settings["missing_unopposed_host_penalty"]
        details["no unopposed host"] = settings["missing_unopposed_host_penalty"]

    if not date.unopposed and "Unopposed" in school.tags:
        requested_penalty = settings["missing_requested_unopposed_penalty"] * priority
        penalty += requested_penalty
        details["missing requested unopposed"] = requested_penalty

    return penalty, details


def _sort_key(date_value):
    month = int(date_value.split("/")[0])
    day = int(date_value.split("/")[1].split("-")[0])
    academic_year = 0 if month >= 8 else 1
    return (academic_year, month, day)


def _build_nodes(schools, dates, seed):
    school_nodes = []
    date_nodes = []

    for school in schools:
        if school.desired_tournaments == 1:
            school_nodes.append(copy.deepcopy(school))
        elif school.desired_tournaments == 2:
            semester_one = copy.deepcopy(school)
            semester_two = copy.deepcopy(school)
            semester_one.semester = 1
            semester_two.semester = 2
            school_nodes.extend([semester_one, semester_two])

    one_to_two_index = 0
    for date in dates:
        num_tournaments = date.num_tournaments
        if isinstance(num_tournaments, list):
            num_tournaments = num_tournaments[(seed >> one_to_two_index) & 1]
            one_to_two_index += 1
        if num_tournaments == 1:
            unopposed = copy.deepcopy(date)
            unopposed.unopposed = True
            unopposed.active_tournament_count = 1
            date_nodes.append(unopposed)
        elif num_tournaments == 2:
            north = copy.deepcopy(date)
            south = copy.deepcopy(date)
            north.region = "North"
            south.region = "South"
            north.active_tournament_count = 2
            south.active_tournament_count = 2
            date_nodes.extend([north, south])
        elif num_tournaments == 3:
            north = copy.deepcopy(date)
            south = copy.deepcopy(date)
            central = copy.deepcopy(date)
            north.region = "North"
            south.region = "South"
            central.region = "Central"
            north.active_tournament_count = 3
            south.active_tournament_count = 3
            central.active_tournament_count = 3
            date_nodes.extend([north, south, central])

    return school_nodes, date_nodes


def _build_matching(schools, dates, seed, settings):
    school_nodes, date_nodes = _build_nodes(schools, dates, seed)
    edges = []
    penalty_lookup = {}

    for date_index, date in enumerate(date_nodes):
        vertex_index = date_index + len(school_nodes)
        for school_index, school in enumerate(school_nodes):
            if school.semester != "any" and school.semester != date.sem:
                continue
            penalty, details = _get_penalty(school, date, settings)
            edges.append((school_index, vertex_index, penalty))
            penalty_lookup[(school_index, vertex_index)] = (penalty, details)

    if not edges:
        raise SchedulerExecutionError("The uploaded inputs do not produce any valid scheduling edges.")

    graph = nx.Graph()
    graph.add_weighted_edges_from(edges)
    pairs = nx.max_weight_matching(graph, maxcardinality=True, weight="weight")

    schedule = {}
    matched_school_indexes = set()
    total_penalty = 0
    nodes = school_nodes + date_nodes

    for first, second in pairs:
        school_node, date_node = sorted((first, second))
        if school_node >= len(school_nodes) or date_node < len(school_nodes):
            continue
        school = nodes[school_node]
        date = nodes[date_node]
        weight, penalties = penalty_lookup[(school_node, date_node)]
        matched_school_indexes.add(school_node)
        total_penalty += weight
        schedule.setdefault(date.date, []).append(
            {
                "weekend_count": date.active_tournament_count or date.num_tournaments,
                "school": school.name,
                "region": date.region,
                "preference": school.availability.get(date.date, ""),
                "weight": weight,
                "penalties": penalties,
            }
        )

    unmatched_schools = [
        school_nodes[index].name
        for index in range(len(school_nodes))
        if index not in matched_school_indexes
    ]

    return schedule, total_penalty, unmatched_schools


def _schedule_to_lines(schedule, dates_lookup, best_penalty, best_seed):
    lines = [f"Total Penalty: {best_penalty} Seed: {best_seed}"]
    for date_value in sorted(schedule.keys(), key=_sort_key):
        assignments = schedule[date_value]
        weekend_count = assignments[0]["weekend_count"] if assignments else 0
        lines.append("")
        lines.append(f"{date_value} ({weekend_count}):")
        for assignment in assignments:
            tags = list(dates_lookup[date_value].tags)
            if isinstance(dates_lookup[date_value].num_tournaments, list) and len(assignments) == 1:
                tags.append("Unopposed")
            tag_text = f" ({', '.join(tags)})" if tags else ""
            penalty_text = ", ".join(
                f"{key}: {value}" for key, value in assignment["penalties"].items()
            )
            lines.append(f"{assignment['school']}{tag_text}: [{penalty_text}]")
    return "\n".join(lines)


def run_scheduler(schools_csv_text, dates_csv_text, settings=None):
    merged_settings = merge_scheduler_settings(settings)
    schools, dates_lookup = load_scheduler_inputs(schools_csv_text, dates_csv_text)
    dates = list(dates_lookup.values())
    scenario_info = count_scheduler_scenarios(dates_lookup)
    scenario_count = scenario_info["scenario_count"]

    def test_seed(seed):
        schedule, total_penalty, unmatched_schools = _build_matching(
            schools,
            dates,
            seed,
            merged_settings,
        )
        return seed, schedule, total_penalty, unmatched_schools

    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=merged_settings["max_workers"]
        ) as executor:
            results = list(executor.map(test_seed, range(scenario_count)))
    except SchedulerDataError:
        raise
    except Exception as exc:
        raise SchedulerExecutionError("Scheduling failed while evaluating scenarios.") from exc

    best_seed, best_schedule, best_penalty, unmatched_schools = max(
        results,
        key=lambda item: item[2],
    )

    serialized_schedule = []
    for date_value in sorted(best_schedule.keys(), key=_sort_key):
        assignments = sorted(
            best_schedule[date_value],
            key=lambda item: (item["region"] or "", item["school"]),
        )
        tags = list(dates_lookup[date_value].tags)
        if isinstance(dates_lookup[date_value].num_tournaments, list) and len(assignments) == 1:
            tags.append("Unopposed")
        serialized_schedule.append(
            {
                "date": date_value,
                "weekend_count": assignments[0]["weekend_count"] if assignments else 0,
                "tags": tags,
                "assignments": [
                    {
                        **assignment,
                        "penalties_text": ", ".join(
                            f"{key}: {value}"
                            for key, value in assignment["penalties"].items()
                        ),
                    }
                    for assignment in assignments
                ],
            }
        )

    return {
        "best_seed": best_seed,
        "best_penalty": best_penalty,
        "unmatched_schools": unmatched_schools,
        "schedule": serialized_schedule,
        "output_text": _schedule_to_lines(
            best_schedule,
            dates_lookup,
            best_penalty,
            best_seed,
        ),
        "summary": {
            "school_count": len(schools),
            "date_count": len(dates_lookup),
            "scheduled_dates": len(serialized_schedule),
            "unmatched_school_count": len(unmatched_schools),
            "flexible_date_count": scenario_info["flexible_date_count"],
            "scenario_count": scenario_count,
        },
    }
