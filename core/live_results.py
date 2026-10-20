from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

import requests
from django.conf import settings
from django.core.cache import cache

from core.models import COTY, NOTY, SOTY, TOTY, QualPoints, QUAL, Tournament
from core.utils.points import novice_points_for_size, speaker_points_for_size, team_points_for_size


LOGGER = logging.getLogger(__name__)
CACHE_KEY = "live-standings:v1"
CACHE_SECONDS = 300
MARKER_LIMITS = {"toty": 5, "soty": 6, "noty": 5, "coty": None}


def _directory_urls():
    configured = [
        value.strip().rstrip("/")
        for value in getattr(settings, "LIVE_MITTAB_SOURCES", "").split(",")
        if value.strip()
    ]
    try:
        response = requests.get(settings.LIVE_MITTAB_DIRECTORY_URL, timeout=5)
        response.raise_for_status()
        for row in response.json().get("tournaments", []):
            url = str(row.get("url") or "").strip().rstrip("/")
            if url:
                configured.append(url)
    except (requests.RequestException, ValueError, AttributeError):
        LOGGER.warning("Could not refresh the Nu-Tab tournament directory", exc_info=True)
    return list(dict.fromkeys(configured))


def _fetch_source(source_url):
    try:
        response = requests.get(f"{source_url}/api/live-results", timeout=8)
        response.raise_for_status()
        payload = response.json()
        if payload.get("schema_version") != 1:
            return None
        return payload
    except (requests.RequestException, ValueError, AttributeError):
        LOGGER.warning("Could not fetch live results from %s", source_url, exc_info=True)
        return None


def fetch_live_sources():
    urls = _directory_urls()
    if not urls:
        return []
    payloads = []
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as executor:
        futures = {executor.submit(_fetch_source, url): url for url in urls}
        for future in as_completed(futures):
            payload = future.result()
            if payload:
                payloads.append(payload)
    return payloads


def _base_marker(entry, label):
    points = float(getattr(entry, f"marker_{label}", 0) or 0)
    tournament = getattr(entry, f"tournament_{label}", None)
    if not points or not tournament:
        return None
    return {
        "points": points,
        "tournament": tournament.display,
        "url": tournament.get_absolute_url(),
        "live": False,
    }


def _base_rows(model, entity_name, marker_count, season):
    rows = []
    labels = ("one", "two", "three", "four", "five", "six")
    queryset = model.objects.filter(season=season).order_by("-points", "place")
    for entry in queryset:
        entity = getattr(entry, entity_name)
        markers = [
            marker for marker in (
                _base_marker(entry, label) for label in labels[:marker_count]
            ) if marker
        ]
        if marker_count == 0 and float(entry.points or 0) > 0:
            markers = [{
                "points": float(entry.points),
                "tournament": "Reviewed season results",
                "url": "",
                "live": False,
            }]
        if entity_name == "team":
            debaters = list(entity.debaters.select_related("school"))
            key = "team:" + ":".join(str(debater.id) for debater in sorted(debaters, key=lambda d: d.id))
            name = entity.name
            detail = " and ".join(debater.name for debater in debaters)
            url = entity.get_absolute_url()
        elif entity_name == "debater":
            key = f"debater:{entity.id}"
            name = entity.name
            detail = entity.school.name if entity.school else "Unaffiliated"
            url = entity.get_absolute_url()
        else:
            key = f"school:{entity.id}"
            name = entity.name
            detail = ""
            url = entity.get_absolute_url()
        rows.append({
            "key": key,
            "name": name,
            "detail": detail,
            "url": url,
            "markers": markers,
            "base_points": float(entry.points or 0),
        })
    return rows


def _live_key(person):
    apda_id = person.get("apda_id")
    if apda_id not in (None, -1, "-1"):
        return f"debater:{apda_id}"
    school = person.get("school") or {}
    name = str(person.get("name") or "").strip().lower()
    school_key = school.get("apda_id") or str(
        school.get("name") or ""
    ).strip().lower()
    return f"debater-name:{name}:{school_key}"


def _team_key(team):
    people = team.get("debaters", [])
    ids = [person.get("apda_id") for person in people]
    if ids and all(value not in (None, -1, "-1") for value in ids):
        return "team:" + ":".join(str(value) for value in sorted(int(value) for value in ids))
    return "team-name:" + ":".join(sorted(_live_key(person) for person in people))


def _add_marker(rows, key, name, detail, marker):
    row = rows.setdefault(key, {
        "key": key,
        "name": name,
        "detail": detail,
        "url": "",
        "markers": [],
        "base_points": 0.0,
    })
    row["markers"].append(marker)


def _type_flags(type_code):
    config = Tournament.TOURNAMENT_TYPES.get(type_code, {})
    return {
        "toty": bool(config.get("toty", False)),
        "soty": bool(config.get("soty", False)),
        "noty": bool(config.get("noty", False)),
        "qual": bool(config.get("qual", False)),
        "autoqual_bar": int(config.get("autoqual_bar", 0) or 0),
    }


def _live_marker(points, tournament, source_url, placement):
    return {
        "points": float(points),
        "tournament": tournament.get("name") or "Live tournament",
        "url": source_url,
        "placement": placement,
        "live": True,
    }


def _apply_live_payload(boards, payload, season, coty_state):
    tournament = payload.get("tournament") or {}
    if not tournament.get("sanctioned") or str(tournament.get("season")) != str(season):
        return False
    try:
        type_code = int(tournament.get("tournament_type", Tournament.POINTS))
        num_teams = int(tournament.get("num_teams") or 0)
        num_novices = int(tournament.get("num_novice_debaters") or 0)
    except (TypeError, ValueError):
        return False
    host = tournament.get("host_school") or {}
    imported_match = Tournament.objects.filter(
        season=season,
        date=tournament.get("date"),
        qual_type=type_code,
    )
    if host.get("apda_id") not in (None, -1, "-1"):
        imported_match = imported_match.filter(host_id=host.get("apda_id"))
    else:
        imported_match = imported_match.filter(name__iexact=tournament.get("name", ""))
    if imported_match.exists():
        return False
    flags = _type_flags(type_code)
    source_url = str(payload.get("source_url") or "").rstrip("/")
    used = False

    for result in payload.get("team_results", []):
        if result.get("division") != "varsity":
            continue
        team = result.get("team") or {}
        debaters = team.get("debaters") or []
        try:
            place = int(result.get("place"))
        except (TypeError, ValueError):
            continue
        key = _team_key(team)
        detail = " and ".join(person.get("name", "") for person in debaters)
        points = team_points_for_size(num_teams, place)
        if flags["toty"] and points > 0:
            _add_marker(
                boards["toty"], key, team.get("name") or detail, detail,
                _live_marker(points, tournament, source_url, place),
            )
            used = True

        for person in debaters:
            school = person.get("school") or {}
            school_id = school.get("apda_id")
            if not flags["qual"] or not school.get("name"):
                continue
            debater_key = _live_key(person)
            state = coty_state[debater_key]
            raw_points = team_points_for_size(num_teams, place)
            capped_delta = min(raw_points, max(0.0, 60.0 - state["points"]))
            state["points"] += raw_points
            school_key = f"school:{school_id}" if school_id not in (None, -1) else f"school-name:{school.get('name', '').lower()}"
            if capped_delta > 0:
                _add_marker(
                    boards["coty"], school_key, school.get("name"), "",
                    _live_marker(capped_delta, tournament, source_url, place),
                )
                used = True
            qualifies = (
                flags["autoqual_bar"] > 0 and place <= flags["autoqual_bar"]
            ) or state["points"] >= float(settings.QUAL_BAR)
            if qualifies and not state["qualified"]:
                state["qualified"] = True
                _add_marker(
                    boards["coty"], school_key, school.get("name"), "",
                    _live_marker(6, tournament, source_url, place),
                )
                used = True

    for result in payload.get("speaker_results", []):
        person = result.get("debater") or {}
        try:
            place = int(result.get("place"))
        except (TypeError, ValueError):
            continue
        school = person.get("school") or {}
        key = _live_key(person)
        division = result.get("division")
        if division == "varsity" and flags["soty"]:
            points = speaker_points_for_size(num_teams, place)
            board = "soty"
        elif division == "novice" and flags["noty"]:
            points = novice_points_for_size(num_novices, place)
            board = "noty"
        else:
            continue
        if points > 0:
            _add_marker(
                boards[board], key, person.get("name") or "Unknown debater",
                school.get("name") or "Unaffiliated",
                _live_marker(points, tournament, source_url, place),
            )
            used = True
    return used


def _finalize(rows, marker_limit):
    finalized = []
    for row in rows.values():
        markers = sorted(row["markers"], key=lambda marker: -marker["points"])
        if marker_limit:
            markers = markers[:marker_limit]
        row["markers"] = markers
        row["marker_slots"] = (
            markers + [None] * (marker_limit - len(markers))
            if marker_limit
            else markers
        )
        row["points"] = round(sum(marker["points"] for marker in markers), 4)
        row["has_live"] = any(marker["live"] for marker in markers)
        if row["points"] > 0:
            finalized.append(row)
    finalized.sort(key=lambda row: (-row["points"], row["name"].lower()))
    previous_points = None
    previous_place = 0
    for index, row in enumerate(finalized, start=1):
        row["place"] = previous_place if row["points"] == previous_points else index
        row["tied"] = row["points"] == previous_points
        if row["place"] == index:
            previous_place = index
        previous_points = row["points"]
    return finalized


def build_live_standings(season=None):
    season = str(season or settings.CURRENT_SEASON)
    cached = cache.get(f"{CACHE_KEY}:{season}")
    if cached is not None:
        return cached

    boards = {
        "toty": {row["key"]: row for row in _base_rows(TOTY, "team", 5, season)},
        "soty": {row["key"]: row for row in _base_rows(SOTY, "debater", 6, season)},
        "noty": {row["key"]: row for row in _base_rows(NOTY, "debater", 5, season)},
        "coty": {row["key"]: row for row in _base_rows(COTY, "school", 0, season)},
    }
    coty_state = defaultdict(lambda: {"points": 0.0, "qualified": False})
    for qual_points in QualPoints.objects.filter(season=season).select_related("debater"):
        coty_state[f"debater:{qual_points.debater_id}"]["points"] = float(qual_points.points)
    for debater_id in QUAL.objects.filter(season=season).values_list("debater_id", flat=True):
        coty_state[f"debater:{debater_id}"]["qualified"] = True

    active_sources = []
    for payload in fetch_live_sources():
        if _apply_live_payload(boards, payload, season, coty_state):
            active_sources.append(payload.get("tournament", {}).get("name", "Tournament"))

    result = {
        "season": season,
        "boards": {
            name: _finalize(rows, MARKER_LIMITS[name])
            for name, rows in boards.items()
        },
        "active_sources": sorted(set(active_sources)),
    }
    cache.set(f"{CACHE_KEY}:{season}", result, CACHE_SECONDS)
    return result
