import os


DEFAULT_SCHEDULER_SETTINGS = {
    "max_workers": max(1, min(8, os.cpu_count() or 4)),
    "already_scheduled_penalty": 1000000000,
    "rank_1_penalty": 10,
    "rank_2_penalty": 0,
    "rank_3_penalty": -10,
    "impossible_penalty": -1000,
    "missing_unopposed_host_penalty": -1000,
    "missing_requested_unopposed_penalty": -100,
    "missing_tag_penalty": -1000,
    "tag_bonus": 10000,
    "north_to_south_penalty": -10000,
    "north_to_central_penalty": -1000,
    "south_to_central_penalty": -100,
    "central_to_north_penalty": -1000,
    "central_to_south_penalty": -100,
    "central_on_two_tournament_weekend_penalty": 0,
    "south_to_north_penalty": -10000,
}


def merge_scheduler_settings(overrides=None):
    merged = dict(DEFAULT_SCHEDULER_SETTINGS)
    for key, value in (overrides or {}).items():
        if key in merged and value not in ("", None):
            merged[key] = value
    return merged


def get_rank_penalties(settings):
    return {
        "Already Scheduled": settings["already_scheduled_penalty"],
        "Rank 1": settings["rank_1_penalty"],
        "Rank 2": settings["rank_2_penalty"],
        "Rank 3": settings["rank_3_penalty"],
        "Impossible": settings["impossible_penalty"],
    }


def get_region_penalties(settings):
    return {
        ("North", "South"): settings["north_to_south_penalty"],
        ("North", "Central"): settings["north_to_central_penalty"],
        ("South", "Central"): settings["south_to_central_penalty"],
        ("Central", "North"): settings["central_to_north_penalty"],
        ("Central", "South"): settings["central_to_south_penalty"],
        ("South", "North"): settings["south_to_north_penalty"],
    }
