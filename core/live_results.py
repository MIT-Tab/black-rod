from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, timedelta
import logging
import threading

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections
from django.db.models import Prefetch

from core.models import COTY, NOTY, SOTY, TOTY, Debater, QualPoints, QUAL, School, Tournament
from core.utils.points import novice_points_for_size, speaker_points_for_size, team_points_for_size


LOGGER = logging.getLogger(__name__)
CACHE_KEY = "live-standings:v2"
CACHE_SECONDS = 300
# Served while one request rebuilds, so a cache miss never fans out per visitor.
STALE_SECONDS = 60 * 60 * 24
LOCK_SECONDS = 60
FETCH_BUDGET_SECONDS = 15
# Tournaments are deduplicated against reviewed imports within this window.
IMPORT_MATCH_DAYS = 7
MARKER_LIMITS = {"toty": 5, "soty": 6, "noty": 5}
QUAL_POINT_CAP = 60.0
QUAL_BONUS = 6.0
AUTOQUAL_LABELS = dict(QUAL.QUAL_TYPES)
LABELS = ("one", "two", "three", "four", "five", "six")
UNKNOWN_IDS = (None, -1, "-1", "")


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
        if response.status_code == 404:
            # Instances deployed before live results existed have no endpoint.
            return None
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
    payloads = {}
    executor = ThreadPoolExecutor(max_workers=min(8, len(urls)))
    try:
        futures = [executor.submit(_fetch_source, url) for url in urls]
        done, not_done = wait(futures, timeout=FETCH_BUDGET_SECONDS)
        if not_done:
            LOGGER.warning("Skipped %s live sources that exceeded the fetch budget", len(not_done))
        for future in done:
            payload = future.result()
            if payload:
                key = str(payload.get("source_url") or "").rstrip("/") or id(payload)
                payloads[key] = payload
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return list(payloads.values())


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


def _base_queryset(model, entity_name, marker_count, season):
    tournaments = [f"tournament_{label}" for label in LABELS[:marker_count]]
    queryset = model.objects.filter(season=season).order_by("-points", "place")
    if entity_name == "team":
        return queryset.select_related("team", *tournaments).prefetch_related(
            Prefetch("team__debaters", queryset=Debater.objects.select_related("school"))
        )
    return queryset.select_related("debater__school", *tournaments)


def _base_rows(model, entity_name, marker_count, season):
    rows = []
    for entry in _base_queryset(model, entity_name, marker_count, season):
        entity = getattr(entry, entity_name)
        markers = [
            marker for marker in (
                _base_marker(entry, label) for label in LABELS[:marker_count]
            ) if marker
        ]
        if entity_name == "team":
            debaters = sorted(entity.debaters.all(), key=lambda d: d.id)
            key = "team:" + ":".join(str(debater.id) for debater in debaters)
            detail = " and ".join(debater.name for debater in debaters)
        else:
            key = f"debater:{entity.id}"
            detail = entity.school.name if entity.school else "Unaffiliated"
        rows.append({
            "key": key,
            "name": entity.name,
            "detail": detail,
            "url": entity.get_absolute_url(),
            "markers": markers,
            "base_points": float(entry.points or 0),
        })
    return rows


def _known_id(value):
    if value in UNKNOWN_IDS:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _payload_people(payloads):
    for payload in payloads:
        for result in payload.get("team_results", []):
            yield from (result.get("team") or {}).get("debaters") or []
        for result in payload.get("speaker_results", []):
            yield result.get("debater") or {}


class _People:
    """Resolve live debaters against Black Rod's own records where possible.

    Black Rod's debater school is authoritative for COTY and TOTY eligibility,
    so known debaters use it instead of the school a tab director entered.
    """

    def __init__(self, payloads):
        people = list(_payload_people(payloads))
        debater_ids = {_known_id(person.get("apda_id")) for person in people} - {None}
        school_ids = {
            _known_id((person.get("school") or {}).get("apda_id")) for person in people
        } - {None}
        self.debaters = {
            debater.id: debater
            # Explicit APDA IDs can refer to temporary/synthetic records too.
            # Their stored affiliation is still authoritative (including null).
            for debater in Debater.all_objects.filter(id__in=debater_ids).select_related("school")
        }
        self.schools = School.all_objects.in_bulk(school_ids)

    def resolve(self, person):
        apda_id = _known_id(person.get("apda_id"))
        known = self.debaters.get(apda_id) if apda_id is not None else None
        if known:
            school = known.school
            return {
                "key": f"debater:{known.id}",
                "name": known.name,
                "last_initial": (known.last_name[:1] or known.first_name[:1] or "?"),
                "url": known.get_absolute_url(),
                "school_key": f"school:{school.id}" if school else None,
                "school_name": school.name if school else "Unaffiliated",
                "school_eligible": bool(school and school.included_in_oty),
            }
        school = person.get("school") or {}
        known_school = self.schools.get(_known_id(school.get("apda_id")))
        school_name = str(school.get("name") or "").strip()
        name = str(person.get("name") or "").strip()
        if known_school:
            school_key = f"school:{known_school.id}"
            school_name = known_school.name
        elif school_name:
            school_key = f"school-name:{school_name.lower()}"
        else:
            school_key = None
        return {
            "key": f"debater-name:{name.lower()}:{school_key or ''}",
            "name": name or "Unknown debater",
            "last_initial": name.split()[-1][:1] if name else "?",
            "url": "",
            "school_key": school_key,
            "school_name": school_name or "Unaffiliated",
            # Schools Black Rod has not seen yet are assumed eligible until imported.
            "school_eligible": bool(
                school_key and (known_school is None or known_school.included_in_oty)
            ),
        }


def _add_marker(rows, identity, marker):
    row = rows.setdefault(identity["key"], {
        "detail": "",
        "url": "",
        **identity,
        "markers": [],
        "base_points": 0.0,
    })
    row["markers"].append(marker)


def _type_flags(type_code, season):
    config = Tournament.TOURNAMENT_TYPES.get(type_code, {})
    autoqual_bar = int(config.get("autoqual_bar", 0) or 0)
    if type_code == Tournament.NATIONALS:
        autoqual_bar = 0
    if type_code in (Tournament.GENDER_MINORITY, Tournament.BIPOC):
        start_season = getattr(settings, "GM_BIPOC_AUTOQUAL_START_SEASON", 2025)
        if int(season) < int(start_season):
            autoqual_bar = 0
    return {
        "toty": bool(config.get("toty", False)),
        "soty": bool(config.get("soty", False)),
        "noty": bool(config.get("noty", False)) and int(season) <= settings.LAST_NOTY_SEASON,
        "qual": bool(config.get("qual", False)),
        "autoqual_bar": autoqual_bar,
    }


def _live_marker(points, tournament, source_url, placement):
    return {
        "points": float(points),
        "tournament": (
            (tournament.get("host_school") or {}).get("name")
            or tournament.get("name")
            or "Live tournament"
        ),
        "url": source_url,
        "placement": placement,
        "live": True,
    }


def _already_imported(tournament, season, type_code, host_id):
    try:
        held_on = date.fromisoformat(str(tournament.get("date")))
    except ValueError:
        return False
    window = timedelta(days=IMPORT_MATCH_DAYS)
    return Tournament.objects.filter(
        season=season,
        qual_type=type_code,
        host_id=host_id,
        date__range=(held_on - window, held_on + window),
    ).exists()


def _coty_debater(coty, debater):
    """Return the COTY breakdown entry for a debater, creating it if needed."""
    if debater["key"] in coty["debaters"]:
        return coty["debaters"][debater["key"]]
    school = coty["schools"].setdefault(debater["school_key"], {
        "key": debater["school_key"],
        "name": debater["school_name"],
        "url": "",
        "debaters": [],
    })
    entry = {
        "key": debater["key"],
        "name": debater["name"],
        "url": debater["url"],
        "base_points": 0.0,
        "points": 0.0,
        "base_qualled": False,
        "qualled": False,
        "quals": [],
    }
    school["debaters"].append(entry)
    coty["debaters"][debater["key"]] = entry
    return entry


def _apply_coty(context, team_debaters, place, qual_points):
    flags = context["flags"]
    if not flags["qual"] and not flags["autoqual_bar"]:
        return False
    used = False
    for debater in team_debaters:
        if not debater["school_key"] or not debater["school_eligible"]:
            continue
        entry = _coty_debater(context["coty"], debater)
        # Qual points only count from qualifying tournaments, but auto-quals
        # (Brandeis, Yale, NorthAms, ...) award the six COTY points regardless.
        if flags["qual"] and qual_points > 0:
            entry["points"] += qual_points
            used = True
        if 0 < place <= flags["autoqual_bar"]:
            label = AUTOQUAL_LABELS.get(context["type_code"], "Auto-qual")
            if label not in [qual["label"] for qual in entry["quals"]]:
                entry["quals"].append({"label": label, "live": True})
            entry["qualled"] = True
            used = True
        elif flags["qual"] and entry["points"] >= float(settings.QUAL_BAR):
            entry["qualled"] = True
    return used


def _apply_live_payload(boards, payload, season, coty, people):
    tournament = payload.get("tournament") or {}
    if not tournament.get("sanctioned") or str(tournament.get("season")) != str(season):
        return False
    host_id = _known_id((tournament.get("host_school") or {}).get("apda_id"))
    if host_id is None:
        # Without a host we cannot tell when the reviewed import lands, which
        # would double count the tournament for as long as it stays deployed.
        return False
    try:
        type_code = int(tournament.get("tournament_type", Tournament.POINTS))
        num_teams = int(tournament.get("num_teams") or 0)
        num_novices = int(tournament.get("num_novice_debaters") or 0)
    except (TypeError, ValueError):
        return False
    if _already_imported(tournament, season, type_code, host_id):
        return False
    flags = _type_flags(type_code, season)
    context = {"boards": boards, "coty": coty, "flags": flags, "type_code": type_code}
    source_url = str(payload.get("source_url") or "").rstrip("/")
    used = False

    for result in payload.get("team_results", []):
        if result.get("division") != "varsity":
            continue
        team = result.get("team") or {}
        try:
            place = int(result.get("place"))
        except (TypeError, ValueError):
            continue

        def marker(points, place=place):
            return _live_marker(points, tournament, source_url, place)

        team_debaters = [people.resolve(person) for person in team.get("debaters") or []]
        detail = " and ".join(debater["name"] for debater in team_debaters)
        hybrid = len({debater["school_key"] for debater in team_debaters}) > 1
        points = team_points_for_size(num_teams, place)
        toty_eligible = (
            flags["toty"] and points > 0 and team_debaters and not hybrid
            and team_debaters[0]["school_eligible"]
        )
        if toty_eligible:
            known_ids = [debater["key"].split(":", 1)[1] for debater in team_debaters
                         if debater["key"].startswith("debater:")]
            if len(known_ids) == len(team_debaters):
                key = "team:" + ":".join(sorted(known_ids, key=int))
            else:
                key = "team-name:" + ":".join(sorted(d["key"] for d in team_debaters))
            _add_marker(
                boards["toty"],
                {
                    "key": key,
                    "name": (
                        team_debaters[0]["school_name"] + " "
                        + "".join(debater["last_initial"] for debater in team_debaters)
                    ),
                    "detail": detail,
                },
                marker(points),
            )
            used = True

        qual_points = team_points_for_size(num_teams, place) if flags["qual"] else 0
        if _apply_coty(context, team_debaters, place, qual_points):
            used = True

    for result in payload.get("speaker_results", []):
        try:
            place = int(result.get("place"))
        except (TypeError, ValueError):
            continue
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
            debater = people.resolve(result.get("debater") or {})
            _add_marker(
                boards[board],
                {
                    "key": debater["key"],
                    "name": debater["name"],
                    "detail": debater["school_name"],
                    "url": debater["url"],
                },
                _live_marker(points, tournament, source_url, place),
            )
            used = True
    return used


def _finalize(rows, marker_limit):
    finalized = []
    for row in rows.values():
        # Highest markers count and display first, matching the reviewed
        # standings; reviewed markers win ties so live data never displaces them.
        markers = sorted(row["markers"], key=lambda marker: (-marker["points"], marker["live"]))
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
    return _place(finalized)


def _place(rows):
    rows.sort(key=lambda row: (-row["points"], row["name"].lower()))
    previous_points = None
    previous_place = 0
    for index, row in enumerate(rows, start=1):
        row["place"] = previous_place if row["points"] == previous_points else index
        row["tied"] = row["points"] == previous_points
        if row["place"] == index:
            previous_place = index
        previous_points = row["points"]
    return rows


def _base_coty(season):
    """Load reviewed COTY standings as the per-debater breakdown the index shows."""
    coty = {"schools": {}, "debaters": {}}
    for row in COTY.objects.filter(season=season).select_related("school"):
        coty["schools"][f"school:{row.school_id}"] = {
            "key": f"school:{row.school_id}",
            "name": row.school.name,
            "url": row.school.get_absolute_url(),
            "debaters": [],
        }

    if season in {str(value) for value in settings.ONLINE_SEASONS}:
        return coty

    def reviewed_debater(debater):
        school = debater.school
        if school is None or not school.included_in_oty:
            return None
        coty["schools"].setdefault(f"school:{school.id}", {
            "key": f"school:{school.id}",
            "name": school.name,
            "url": school.get_absolute_url(),
            "debaters": [],
        })
        return _coty_debater(coty, {
            "key": f"debater:{debater.id}",
            "name": debater.name,
            "url": debater.get_absolute_url(),
            "school_key": f"school:{school.id}",
            "school_name": school.name,
        })

    for qual_points in QualPoints.objects.filter(season=season).select_related(
        "debater__school"
    ):
        entry = reviewed_debater(qual_points.debater)
        if entry is not None:
            entry["base_points"] = entry["points"] = float(qual_points.points)
    for qual in QUAL.objects.filter(season=season).select_related("debater__school"):
        entry = reviewed_debater(qual.debater)
        if entry is None:
            continue
        entry["base_qualled"] = entry["qualled"] = True
        if qual.qual_type > 0:
            entry["quals"].append({"label": qual.get_qual_type_display(), "live": False})
    return coty


def _contribution(points, qualled):
    return min(QUAL_POINT_CAP, points) + (QUAL_BONUS if qualled else 0.0)


def _finalize_coty(coty):
    """Total schools exactly like update_qual_points: capped points plus quals."""
    schools = []
    for school in coty["schools"].values():
        debaters = []
        for entry in school["debaters"]:
            if entry["points"] <= 0 and not entry["qualled"]:
                continue
            entry["contribution"] = _contribution(entry["points"], entry["qualled"])
            entry["points_live"] = entry["points"] != entry["base_points"]
            entry["qualled_live"] = entry["qualled"] and not entry["base_qualled"]
            entry["contribution_live"] = entry["contribution"] != _contribution(
                entry["base_points"], entry["base_qualled"]
            )
            debaters.append(entry)
        debaters.sort(key=lambda entry: (-entry["points"], entry["name"].lower()))
        school["debaters"] = debaters
        school["points"] = round(sum(entry["contribution"] for entry in debaters), 4)
        school["has_live"] = any(
            entry["points_live"] or entry["qualled_live"] or entry["contribution_live"]
            for entry in debaters
        )
        if school["points"] > 0:
            schools.append(school)
    return _place(schools)


def _compute_live_standings(season):
    """Build the overlay. Reads the database only; nothing here may write."""
    boards = {
        "toty": {row["key"]: row for row in _base_rows(TOTY, "team", 5, season)},
        "soty": {row["key"]: row for row in _base_rows(SOTY, "debater", 6, season)},
        "noty": {row["key"]: row for row in _base_rows(NOTY, "debater", 5, season)},
    }
    coty = _base_coty(season)

    payloads = fetch_live_sources()
    people = _People(payloads)
    is_online_season = season in {str(value) for value in settings.ONLINE_SEASONS}
    active_sources = []
    for payload in payloads:
        if is_online_season:
            break
        if _apply_live_payload(boards, payload, season, coty, people):
            active_sources.append(payload.get("tournament", {}).get("name", "Tournament"))

    return {
        "season": season,
        "boards": {
            **{
                name: _finalize(rows, MARKER_LIMITS[name])
                for name, rows in boards.items()
            },
            "coty": _finalize_coty(coty),
        },
        "active_sources": sorted(set(active_sources)),
    }


def _cache_set(key, value, timeout):
    try:
        cache.set(key, value, timeout)
    except Exception:  # pylint: disable=broad-except
        # An oversized or unreachable cache must not turn into a 500 or a
        # refetch storm; the stale copy keeps serving in the meantime.
        LOGGER.exception("Could not cache live standings under %s", key)


def cached_live_standings(season=None):
    """Return the last computed live standings without waiting on a refresh.

    When the fresh copy has expired, one background refresh is started so pages
    like the index can advertise live results without visitors loading /live.
    """
    season = str(season or settings.CURRENT_SEASON)
    fresh_key = f"{CACHE_KEY}:{season}"
    cached = cache.get(fresh_key)
    if cached is not None:
        return cached
    if cache.add(f"{fresh_key}:lock", 1, LOCK_SECONDS):
        _start_refresh(season)
    return cache.get(f"{fresh_key}:stale")


def _refresh(season):
    fresh_key = f"{CACHE_KEY}:{season}"
    try:
        result = _compute_live_standings(season)
        _cache_set(fresh_key, result, CACHE_SECONDS)
        _cache_set(f"{fresh_key}:stale", result, STALE_SECONDS)
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception("Background live standings refresh failed")
    finally:
        cache.delete(f"{fresh_key}:lock")
        close_old_connections()


def _start_refresh(season):
    threading.Thread(target=_refresh, args=(season,), daemon=True).start()


def invalidate_live_standings(season):
    """Drop cached live standings once reviewed results change."""
    fresh_key = f"{CACHE_KEY}:{season}"
    cache.delete_many([fresh_key, f"{fresh_key}:stale"])


def build_live_standings(season=None):
    season = str(season or settings.CURRENT_SEASON)
    fresh_key = f"{CACHE_KEY}:{season}"
    stale_key = f"{fresh_key}:stale"
    lock_key = f"{fresh_key}:lock"

    cached = cache.get(fresh_key)
    if cached is not None:
        return cached

    stale = cache.get(stale_key)
    if not cache.add(lock_key, 1, LOCK_SECONDS) and stale is not None:
        # Someone else is refreshing; serve the previous standings.
        return stale

    try:
        result = _compute_live_standings(season)
    finally:
        cache.delete(lock_key)
    _cache_set(fresh_key, result, CACHE_SECONDS)
    _cache_set(stale_key, result, STALE_SECONDS)
    return result
