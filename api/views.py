from collections import defaultdict
from datetime import date as date_class, timedelta
from functools import lru_cache
import html
import json
import logging
from pathlib import Path
from urllib.parse import unquote_plus

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, F, Max, Prefetch, Q
from django.http import Http404, HttpResponse
from django.test import RequestFactory
from django.urls import resolve, reverse, Resolver404
from django.views import View
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
    extend_schema_view,
)
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models.debater import Debater, Reaff
from core.models.results.speaker import SpeakerResult
from core.models.results.team import TeamResult
from core.models.round import Round
from core.models.school import School
from core.models.standings.coty import COTY
from core.models.standings.noty import NOTY
from core.models.standings.online_qual import OnlineQUAL
from core.models.standings.qual import QUAL
from core.models.standings.soty import SOTY
from core.models.standings.toty import TOTY, TOTYReaff
from core.models.team import Team
from core.models.tournament import Tournament
from core.models.video import Video
from core.utils.perms import has_perm
from core.utils.rankings import get_qualled_debaters, place_as_round
from core.utils.rounds import get_record, get_tab_card_data
from core.utils.schools import get_debaters_for_season
from .serializers import (
    serialize_debater,
    serialize_school,
    serialize_speaker_result,
    serialize_team,
    serialize_team_result,
    serialize_tournament,
    serialize_video,
)
from .schema_serializers import (
    DebaterDetailResponseSerializer,
    ErrorResponseSerializer,
    OTYGuideResponseSerializer,
    ScheduleResponseSerializer,
    SchoolDebatersResponseSerializer,
    SchoolDetailResponseSerializer,
    SchoolListResponseSerializer,
    SeasonStandingsReplayResponseSerializer,
    SeasonStandingsResponseSerializer,
    StandingsThroughDateResponseSerializer,
    TeamDetailResponseSerializer,
    TournamentDetailResponseSerializer,
)


# ----------------------------------------------------------------------
# Helper constants shared by multiple AI-friendly endpoints
# ----------------------------------------------------------------------

MARKER_LABELS = ["one", "two", "three", "four", "five", "six"]
REPLAY_MARKER_LIMITS = {"toty": 5, "soty": 6, "coty": 0}
LLM_PROXY_ALLOWED_PREFIXES = ("/api/", "/.well-known/")
LLM_PROXY_ALLOWED_PATHS = {"/ai-plugin.json", "/openapi.json"}
LOGGER = logging.getLogger(__name__)
OTY_GUIDE_PATH = Path(__file__).resolve().parent / "content" / "oty_guide.md"
SEASON_QUERY_PARAM = OpenApiParameter(
    name="season",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.QUERY,
    description="APDA season (e.g., '2024'). Defaults to the current season.",
)
THROUGH_DATE_PARAM = OpenApiParameter(
    name="through",
    type=OpenApiTypes.DATE,
    location=OpenApiParameter.QUERY,
    description="ISO-8601 date (YYYY-MM-DD). Only markers earned on/before this date are counted.",
    required=True,
)
SCHOOL_ID_PARAM = OpenApiParameter(
    name="school_id",
    type=OpenApiTypes.INT,
    location=OpenApiParameter.PATH,
    description="Primary key of the school.",
    required=True,
)
DEBATER_ID_PARAM = OpenApiParameter(
    name="pk",
    type=OpenApiTypes.INT,
    location=OpenApiParameter.PATH,
    description="Primary key referenced in the URL.",
    required=True,
)
TEAM_ID_PARAM = OpenApiParameter(
    name="pk",
    type=OpenApiTypes.INT,
    location=OpenApiParameter.PATH,
    description="Primary key of the team.",
    required=True,
)
TOURNAMENT_ID_PARAM = OpenApiParameter(
    name="pk",
    type=OpenApiTypes.INT,
    location=OpenApiParameter.PATH,
    description="Primary key of the tournament.",
    required=True,
)
SCHOOL_PK_PARAM = OpenApiParameter(
    name="pk",
    type=OpenApiTypes.INT,
    location=OpenApiParameter.PATH,
    description="Primary key of the school.",
    required=True,
)
BOARD_PARAM = OpenApiParameter(
    name="board",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.QUERY,
    description="Repeat or comma-separate to limit standings boards (toty, soty, coty, noty, online_quals).",
    required=False,
)
LIMIT_PARAM = OpenApiParameter(
    name="limit",
    type=OpenApiTypes.INT,
    location=OpenApiParameter.QUERY,
    description="Maximum number of entries to return per board (defaults to all).",
    required=False,
)
ENTRY_LIMIT_PARAM = OpenApiParameter(
    name="limit",
    type=OpenApiTypes.INT,
    location=OpenApiParameter.QUERY,
    description="Maximum number of repeated entries to return (defaults to all).",
    required=False,
)


@extend_schema_view(
    get=extend_schema(
        summary="List active schools",
        description="Return the 25 schools with the most active debaters in the past two seasons.",
        responses=SchoolListResponseSerializer,
    )
)
class ActiveSchoolListAPIView(APIView):
    """
    API endpoint to list schools with recent activity.

    GET /api/schools/
    Returns: Top 25 schools by number of active debaters in last 2 years
    Cached for 5 minutes.
    """

    def get(self, request):
        cache_key = 'api:active_schools'
        cached_data = cache.get(cache_key)

        if cached_data is not None:
            return Response(cached_data)

        current_year = int(settings.CURRENT_SEASON)
        cutoff_season = str(current_year - 2)

        schools = School.objects.filter(
            debaters__latest_season__gte=cutoff_season
        ).annotate(
            active_debater_count=Count(
                'debaters',
                filter=Q(debaters__latest_season__gte=cutoff_season)
            )
        ).order_by('-active_debater_count', 'name')[:25]

        data = {
            "count": len(schools),
            "schools": [serialize_school(school, request) for school in schools]
        }

        cache.set(cache_key, data, 300)

        return Response(data)


@extend_schema_view(
    get=extend_schema(
        summary="List all schools",
        description="Return every school in the database, ordered by most recent debater activity.",
        responses=SchoolListResponseSerializer,
    )
)
class AllSchoolListAPIView(APIView):
    """
    API endpoint to list all schools.

    GET /api/schools/all/
    Returns: List of all schools in the database
    Cached for 5 minutes.
    """

    def get(self, request):
        cache_key = 'api:all_schools:recent_competition_date'
        cached_data = cache.get(cache_key)

        if cached_data is not None:
            return Response(cached_data)

        schools = list(
            School.objects.annotate(
                latest_competed_at=Max('debaters__teams__team_results__tournament__date'),
                latest_competed_season=Max('debaters__latest_season'),
            ).order_by(
                F('latest_competed_at').desc(nulls_last=True),
                F('latest_competed_season').desc(nulls_last=True),
                'name',
            )
        )

        data = {
            "count": len(schools),
            "schools": [serialize_school(school, request) for school in schools]
        }
        cache.set(cache_key, data, 300)

        return Response(data)


@extend_schema_view(
    get=extend_schema(
        summary="List debaters for a school",
        parameters=[SCHOOL_ID_PARAM],
        description="Return debaters from the specified school who have competed within the last five seasons.",
        responses={200: SchoolDebatersResponseSerializer, 404: ErrorResponseSerializer},
    )
)
class SchoolDebatersAPIView(APIView):
    """
    API endpoint to list debaters from a specific school.

    GET /api/debaters/<school_id>/
    Returns: List of debaters active in the last 5 years for the specified school
    Cached for 5 minutes per school.
    """

    def get(self, request, school_id):
        cache_key = f'api:school_debaters:{school_id}:recent_competition_date'
        cached_data = cache.get(cache_key)

        if cached_data is not None:
            return Response(cached_data)

        try:
            school = School.objects.get(id=school_id)
        except School.DoesNotExist as exc:
            raise Http404("School not found") from exc

        current_year = int(settings.CURRENT_SEASON)
        cutoff_year = current_year - 5
        cutoff_season = str(cutoff_year)

        debaters = Debater.objects.filter(
            school=school,
            latest_season__gte=cutoff_season
        ).select_related('school').annotate(
            latest_competed_at=Max('teams__team_results__tournament__date')
        ).order_by(
            F('latest_competed_at').desc(nulls_last=True),
            '-latest_season',
            'last_name',
            'first_name',
        )
        debaters = list(debaters)

        data = {
            "school": serialize_school(school, request),
            "count": len(debaters),
            "debaters": [serialize_debater(debater, request) for debater in debaters]
        }

        cache.set(cache_key, data, 300)

        return Response(data)


# ----------------------------------------------------------------------
# Helper functions shared by multiple AI-friendly endpoints
# ----------------------------------------------------------------------

def _parse_multi_value_param(raw_values):
    values = []
    seen = set()
    for raw in raw_values:
        for part in raw.split(","):
            normalized = part.strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            values.append(normalized)
    return values


def _selected_boards(request, allowed_boards):
    selections = _parse_multi_value_param(request.GET.getlist("board"))
    selected = [board for board in selections if board in allowed_boards]
    return selected or allowed_boards


def _parse_limit_param(request, param_name="limit", default=None, max_limit=200):
    raw = request.GET.get(param_name)
    if not raw:
        return default
    try:
        value = int(raw)
        if value <= 0:
            return default
        return min(value, max_limit)
    except ValueError:
        return default


def _trim_entries(entries, limit):
    if limit is None:
        return entries
    return entries[:limit]


def _filter_standings_payload(standings, selected_boards, limit):
    filtered = {}
    for board in selected_boards:
        if board not in standings:
            continue
        filtered[board] = _trim_entries(standings[board], limit)
    return filtered


def _current_endpoint_path(request):
    full_path = request.get_full_path()
    if not full_path.startswith("/"):
        return f"/{full_path}"
    return full_path


def _available_season_values():
    return [season[0] for season in settings.SEASONS]


@lru_cache(maxsize=1)
def _load_oty_guide_text():
    try:
        return OTY_GUIDE_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        LOGGER.warning("OTY guide file missing at %s", OTY_GUIDE_PATH)
        return ""


def _oty_guide_last_modified():
    try:
        timestamp = OTY_GUIDE_PATH.stat().st_mtime
    except OSError:
        return None
    return date_class.fromtimestamp(timestamp).isoformat()


def _format_season_display(season):
    season_map = dict(settings.SEASONS)
    if season in season_map:
        return season_map[season]
    try:
        return f"{season}-{str(int(season) + 1)[2:]}"
    except (TypeError, ValueError):
        return str(season)


def _resolve_season(request):
    season = request.GET.get("season", settings.CURRENT_SEASON)
    if season not in _available_season_values():
        season = settings.CURRENT_SEASON
    return season


def _round_result(round_obj, team):
    gov_wins = {Round.GOV, Round.GOV_VIA_FORFEIT, Round.ALL_WIN}
    opp_wins = {Round.OPP, Round.OPP_VIA_FORFEIT, Round.ALL_WIN}

    victor = round_obj.victor
    if round_obj.gov_id == team.id and victor in gov_wins:
        if victor == Round.GOV_VIA_FORFEIT:
            return "WF"
        if victor == Round.ALL_WIN:
            return "AW"
        return "W"

    if round_obj.opp_id == team.id and victor in opp_wins:
        if victor == Round.OPP_VIA_FORFEIT:
            return "WF"
        if victor == Round.ALL_WIN:
            return "AW"
        return "W"

    if victor == Round.ALL_DROP:
        return "AL"
    if victor > Round.OPP:
        return "LF"
    return "L"


def _serialize_round_stat(stat, request):
    if not stat:
        return None
    return {
        "debater": serialize_debater(stat.debater, request),
        "role": (stat.debater_role or "").upper(),
        "speaks": float(stat.speaks) if stat.speaks is not None else None,
        "ranks": float(stat.ranks) if stat.ranks is not None else None,
    }


def _llm_proxy_url(request, endpoint_path):
    proxy_path = reverse("llm_proxy")
    return request.build_absolute_uri(f"{proxy_path}?endpoint={endpoint_path}")


class LLMProxyPayloadError(Exception):
    """Raised when an internal endpoint returns a successful non-JSON payload."""


def _extract_json_payload(response):
    if hasattr(response, "data"):
        return response.data

    if hasattr(response, "render") and callable(response.render):
        rendered_response = response.render()
        if rendered_response is not None:
            response = rendered_response

    raw_content = response.content.decode(
        getattr(response, "charset", "utf-8") or "utf-8",
        errors="replace",
    )

    try:
        return json.loads(raw_content)
    except json.JSONDecodeError as exc:
        content_type = response.get("Content-Type", "")
        raise LLMProxyPayloadError(
            f"Endpoint returned non-JSON content with Content-Type {content_type or 'unknown'}."
        ) from exc


def _serialize_tab_card(team, tournament, request):
    tab_card_rows = get_tab_card_data(team, tournament)
    serialized = []
    if not tab_card_rows:
        return serialized

    for entry in tab_card_rows:
        round_obj = entry["round"]
        opponent = round_obj.opp if round_obj.gov_id == team.id else round_obj.gov
        serialized.append(
            {
                "round_id": round_obj.id,
                "round_number": round_obj.round_number,
                "opponent": serialize_team(opponent, request, include_debaters=False),
                "opponent_side": "OPP" if round_obj.gov_id == team.id else "GOV",
                "result": _round_result(round_obj, team),
                "stats": [
                    stat_payload
                    for stat_payload in (
                        _serialize_round_stat(stat, request)
                        for stat in entry["stats"]
                    )
                    if stat_payload
                ],
            }
        )
    return serialized


def _lite_school(school, request):
    if not school:
        return None
    data = {
        "id": school.id,
        "name": school.name,
    }
    if request:
        data["url"] = request.build_absolute_uri(school.get_absolute_url())
    return data


def _lite_debater(debater, request):
    if not debater:
        return None
    data = {
        "id": debater.id,
        "name": debater.name,
        "school_id": debater.school_id,
        "school_name": debater.school.name if debater.school else None,
    }
    if request:
        data["url"] = request.build_absolute_uri(debater.get_absolute_url())
    return data


def _lite_team(team, request):
    if not team:
        return None
    data = {
        "id": team.id,
        "name": team.name,
        "debaters": [_lite_debater(debater, request) for debater in team.debaters.all()],
    }
    if request:
        data["url"] = request.build_absolute_uri(team.get_absolute_url())
    return data


def _lite_tournament(tournament, request):
    if not tournament:
        return None
    data = {
        "id": tournament.id,
        "name": tournament.name,
        "display": tournament.display,
        "date": tournament.date.isoformat() if tournament.date else None,
    }
    if request:
        data["url"] = request.build_absolute_uri(tournament.get_absolute_url())
    return data


def _tournament_special_notes(tournament):
    name = (tournament.name or "").lower()
    notes = []
    is_gm_event = (
        tournament.qual_type == Tournament.GENDER_MINORITY
        or "gender minority" in name
        or "gm" in name.split()
    )
    is_bipoc_event = tournament.qual_type == Tournament.BIPOC or "bipoc" in name
    gm_bipoc_autoqual_active = (
        tournament.gm_bipoc_autoqual_enabled()
        if hasattr(tournament, "gm_bipoc_autoqual_enabled")
        else True
    )

    if tournament.qual_type == Tournament.EXPANSION or "bp" in name:
        notes.append(
            "British Parliamentary weekends award autoqual slots, but no TOTY/SOTY/COTY points."
        )
    if tournament.qual_type == Tournament.NATIONALS or "nationals" in name:
        notes.append(
            "Nationals is championship-only: it does not award season points, only a title and autoqual bids."
        )
    if is_gm_event:
        gm_note = "Gender Minority invitationals award COTY/qual points only"
        if gm_bipoc_autoqual_active:
            gm_note += " and autoqual finalists."
        notes.append(gm_note)
    if is_bipoc_event:
        bipoc_note = "BIPOC invitationals award COTY/qual points only"
        if gm_bipoc_autoqual_active:
            bipoc_note += " and autoqual finalists."
        notes.append(bipoc_note)
    return notes


def _tournament_oty_payload(tournament):
    name = (tournament.name or "").lower()
    toty_points = bool(tournament.toty)
    soty_points = bool(tournament.soty)
    coty_points = bool(tournament.qual)

    if tournament.qual_type in {Tournament.EXPANSION, Tournament.NATIONALS} or "nationals" in name or "bp" in name:
        toty_points = False
        soty_points = False
        coty_points = False

    if (
        tournament.qual_type == Tournament.GENDER_MINORITY
        or tournament.qual_type == Tournament.BIPOC
        or "gender minority" in name
        or "gm" in name.split()
        or "bipoc" in name
    ):
        toty_points = False
        soty_points = False
        coty_points = True

    return {
        "toty_points": toty_points,
        "soty_points": soty_points,
        "coty_points": coty_points,
        "qual_type": tournament.get_qual_type_display(),
        "autoqual_bar": tournament.autoqual_bar,
        "notes": _tournament_special_notes(tournament),
    }


def _schedule_tournament_sort_key(tournament):
    priority = 1 if tournament.qual_type in {Tournament.BRANDEIS, Tournament.YALE} else 0
    return (priority, tournament.qual_type or 0, tournament.name)


def _finalize_week_block(week, request):
    tournaments = week.pop("entries", [])
    tournaments.sort(key=_schedule_tournament_sort_key)
    week["tournaments"] = [
        {
            "tournament": serialize_tournament(tournament, request),
            "otys": _tournament_oty_payload(tournament),
        }
        for tournament in tournaments
    ]
    return week


def _schedule_month_blocks(tournaments, request):
    months = defaultdict(list)
    for tournament in tournaments:
        if not tournament.date:
            continue
        key = (tournament.date.year, tournament.date.month)
        months[key].append(tournament)

    month_blocks = []
    for (year, month), month_tournaments in sorted(months.items()):
        month_tournaments.sort(key=lambda t: t.date.day)
        weeks = []
        current_week = None
        for tournament in month_tournaments:
            day = tournament.date.day
            if not current_week or day != current_week["date"]:
                if current_week:
                    weeks.append(_finalize_week_block(current_week, request))
                current_week = {
                    "date": day,
                    "one_more": (tournament.date + timedelta(days=1)).day,
                    "entries": [],
                }
            current_week["entries"].append(tournament)

        if current_week:
            weeks.append(_finalize_week_block(current_week, request))

        weeks.sort(key=lambda week: week["date"])

        month_blocks.append(
            {
                "month": month,
                "display": month_tournaments[0].date.strftime("%B"),
                "year": year,
                "weeks": weeks,
            }
        )
    month_blocks.sort(key=lambda block: (block["year"], block["month"]))
    return month_blocks


def _serialize_markers(obj, marker_count, request):
    markers = []
    for position, label in enumerate(MARKER_LABELS[:marker_count], start=1):
        points = getattr(obj, f"marker_{label}", 0)
        tournament = getattr(obj, f"tournament_{label}", None)
        if points and tournament:
            markers.append(
                {
                    "slot": position,
                    "points": points,
                    "tournament": _lite_tournament(tournament, request),
                }
            )
    return markers


def _sort_markers(markers):
    def sort_key(marker):
        earned_on = marker.get("earned_on") or ""
        # Sort by date, then by descending points to keep deterministic ordering.
        return (earned_on, -marker.get("points", 0), marker.get("result_id", 0))

    return sorted(markers, key=sort_key)


def _get_team_results_for_replay(team, season):
    if hasattr(team, "season_replay_results"):
        return list(team.season_replay_results)
    return list(
        team.team_results.filter(
            tournament__season=season,
            tournament__toty=True,
            type_of_place=Debater.VARSITY,
        ).select_related("tournament")
    )


def _serialize_team_results_for_replay(results, season, request, source_team_id, from_reaff=False):
    serialized = []
    for result in results:
        tournament = result.tournament
        if not tournament:
            continue
        points = tournament.get_toty_points(
            result.place, ghost_points=result.ghost_points
        )
        if points <= 0:
            continue
        serialized.append(
            {
                "points": points,
                "place": result.place,
                "ghost_points": result.ghost_points,
                "type": result.get_type_of_place_display(),
                "earned_on": tournament.date.isoformat() if tournament.date else None,
                "tournament": _lite_tournament(tournament, request),
                "result_id": result.id,
                "source_team_id": source_team_id,
                "from_reaff": from_reaff,
            }
        )
    return serialized


def _team_replay_markers(entry, season, request):
    team = entry.team
    if not team:
        return []

    markers = _serialize_team_results_for_replay(
        _get_team_results_for_replay(team, season),
        season,
        request,
        team.id,
        from_reaff=False,
    )

    for reaff in getattr(team, "season_reaffs", []):
        old_team = getattr(reaff, "old_team", None)
        if not old_team:
            continue
        old_results = _get_team_results_for_replay(old_team, season)
        markers.extend(
            _serialize_team_results_for_replay(
                old_results,
                season,
                request,
                old_team.id,
                from_reaff=True,
            )
        )

    return _sort_markers(markers)


def _get_speaker_results_for_replay(debater, season):
    if hasattr(debater, "season_replay_results"):
        return list(debater.season_replay_results)
    return list(
        debater.speaker_results.filter(
            tournament__season=season,
            tournament__soty=True,
            type_of_place=Debater.VARSITY,
        ).select_related("tournament")
    )


def _serialize_speaker_results_for_replay(results, season, request, source_debater_id, from_reaff=False):
    serialized = []
    for result in results:
        tournament = result.tournament
        if not tournament:
            continue
        place_adjustment = result.place - (1 if result.tie else 0)
        points = tournament.get_soty_points(place_adjustment)
        if points <= 0:
            continue
        serialized.append(
            {
                "points": points,
                "place": result.place,
                "tie": result.tie,
                "type": result.get_type_of_place_display(),
                "earned_on": tournament.date.isoformat() if tournament.date else None,
                "tournament": _lite_tournament(tournament, request),
                "result_id": result.id,
                "source_debater_id": source_debater_id,
                "from_reaff": from_reaff,
            }
        )
    return serialized


def _speaker_replay_markers(entry, season, request):
    debater = entry.debater
    if not debater:
        return []

    markers = _serialize_speaker_results_for_replay(
        _get_speaker_results_for_replay(debater, season),
        season,
        request,
        debater.id,
    )

    for reaff in getattr(debater, "season_reaffs", []):
        old_debater = getattr(reaff, "old_debater", None)
        if not old_debater:
            continue
        old_results = _get_speaker_results_for_replay(old_debater, season)
        markers.extend(
            _serialize_speaker_results_for_replay(
                old_results,
                season,
                request,
                old_debater.id,
                from_reaff=True,
            )
        )

    return _sort_markers(markers)


def _coty_qual_label(qual_type):
    labels = dict(QUAL.QUAL_TYPES)
    if qual_type == Tournament.POINTS:
        return labels.get(QUAL.POINTS, "Points")
    return labels.get(qual_type, dict(Tournament.QUAL_TYPES).get(qual_type, "Qual"))


def _coty_marker_sort_key(marker):
    marker_order = {"qual_points": 0, "qual_bonus": 1}
    return (
        marker.get("earned_on") or "",
        marker.get("tournament", {}).get("id") or 0,
        marker.get("result_id") or 0,
        marker.get("debater", {}).get("id") or 0,
        marker_order.get(marker.get("marker_type"), 99),
    )


def _sort_coty_markers(markers):
    return sorted(markers, key=_coty_marker_sort_key)


def _coty_replay_marker(
    *,
    marker_type,
    points,
    result,
    debater,
    request,
    raw_points=None,
    qualification=None,
):
    tournament = result.tournament
    marker = {
        "points": round(float(points), 4),
        "marker_type": marker_type,
        "place": result.place,
        "ghost_points": result.ghost_points,
        "earned_on": tournament.date.isoformat() if tournament.date else None,
        "tournament": _lite_tournament(tournament, request),
        "result_id": result.id,
        "debater": _lite_debater(debater, request),
    }
    if raw_points is not None:
        marker["raw_points"] = round(float(raw_points), 4)
    if qualification:
        marker["qualification"] = qualification
    return marker


def _collect_coty_replay_standings(season, request):
    """
    Derive COTY replay state from raw results without touching persisted rankings.

    Normal COTY rebuilds intentionally mutate QUAL, QualPoints, COTY, and cache state.
    Replay cannot use that path because snapshots need qualification bonuses on the
    date they became live and because API reads must not repair or rewrite rankings.
    """
    if str(season) in {str(value) for value in settings.ONLINE_SEASONS}:
        return [], []

    team_debaters_prefetch = Prefetch(
        "team__debaters",
        queryset=Debater.objects.filter(
            school__isnull=False,
            school__included_in_oty=True,
        ).select_related("school"),
        to_attr="coty_replay_debaters",
    )
    results = (
        TeamResult.objects.filter(
            tournament__season=season,
            type_of_place=Debater.VARSITY,
            counts_for_points=True,
        )
        .select_related("team", "tournament")
        .prefetch_related(team_debaters_prefetch)
        .order_by("tournament__date", "tournament_id", "id")
    )

    debater_state = {}
    school_markers = defaultdict(list)
    schools = {}
    qual_bar = float(settings.QUAL_BAR)

    for result in results:
        tournament = result.tournament
        if not tournament:
            continue

        debaters = getattr(result.team, "coty_replay_debaters", [])
        for debater in debaters:
            school = debater.school
            if not school:
                continue

            schools[school.id] = school
            state = debater_state.setdefault(
                debater.id,
                {
                    "school_id": school.id,
                    "raw_points": 0.0,
                    "capped_points": 0.0,
                    "qualled": False,
                },
            )

            raw_points = tournament.get_qual_points(
                result.place, ghost_points=result.ghost_points
            )
            if raw_points > 0:
                capped_room = max(0.0, 60.0 - state["capped_points"])
                capped_delta = min(float(raw_points), capped_room)
                state["raw_points"] += float(raw_points)
                if capped_delta > 0:
                    state["capped_points"] += capped_delta
                    school_markers[school.id].append(
                        _coty_replay_marker(
                            marker_type="qual_points",
                            points=capped_delta,
                            raw_points=raw_points,
                            result=result,
                            debater=debater,
                            request=request,
                        )
                    )

            autoqual = (
                tournament.qual_type != Tournament.NATIONALS
                and result.place != -1
                and tournament.autoqual_bar > 0
                and result.place <= tournament.autoqual_bar
            )
            points_qual = state["raw_points"] >= qual_bar

            if not state["qualled"] and (autoqual or points_qual):
                state["qualled"] = True
                qualification_type = (
                    tournament.qual_type if autoqual else Tournament.POINTS
                )
                school_markers[school.id].append(
                    _coty_replay_marker(
                        marker_type="qual_bonus",
                        points=6,
                        result=result,
                        debater=debater,
                        request=request,
                        qualification={
                            "type": qualification_type,
                            "label": _coty_qual_label(qualification_type),
                        },
                    )
                )

    rows = []
    timeline_dates = set()
    for school_id, markers in school_markers.items():
        sorted_markers = _sort_coty_markers(markers)
        points = round(sum(_marker_points(marker) for marker in sorted_markers), 4)
        if points <= 0:
            continue
        for marker in sorted_markers:
            earned_on = marker.get("earned_on")
            if earned_on:
                timeline_dates.add(earned_on)
        school = schools.get(school_id)
        if not school:
            continue
        rows.append(
            {
                "season": str(season),
                "season_display": _format_season_display(season),
                "place_display": "",
                "points": points,
                "place": 0,
                "tied": False,
                "school": _lite_school(school, request),
                "markers": [],
                "all_markers": sorted_markers,
                "_sort_name": school.name.lower(),
            }
        )

    rows.sort(key=lambda row: (-row["points"], row["_sort_name"]))
    _assign_places(rows)
    for row in rows:
        row["place_display"] = row["place"]
        row.pop("_sort_name", None)

    return rows, sorted(timeline_dates)


def _collect_replay_standings(season, request):
    team_results_prefetch = Prefetch(
        "team__team_results",
        queryset=TeamResult.objects.filter(
            tournament__season=season,
            tournament__toty=True,
            type_of_place=Debater.VARSITY,
        ).select_related("tournament"),
        to_attr="season_replay_results",
    )

    toty_reaff_prefetch = Prefetch(
        "team__toty_reaff_new",
        queryset=TOTYReaff.objects.filter(season=season)
        .select_related("old_team")
        .prefetch_related(
            Prefetch(
                "old_team__team_results",
                queryset=TeamResult.objects.filter(
                    tournament__season=season,
                    tournament__toty=True,
                    type_of_place=Debater.VARSITY,
                ).select_related("tournament"),
                to_attr="season_replay_results",
            )
        ),
        to_attr="season_reaffs",
    )

    speaker_results_prefetch = Prefetch(
        "debater__speaker_results",
        queryset=SpeakerResult.objects.filter(
            tournament__season=season,
            tournament__soty=True,
            type_of_place=Debater.VARSITY,
        ).select_related("tournament"),
        to_attr="season_replay_results",
    )

    reaff_prefetch = Prefetch(
        "debater__reaff_new",
        queryset=Reaff.objects.filter(season=season)
        .select_related("old_debater")
        .prefetch_related(
            Prefetch(
                "old_debater__speaker_results",
                queryset=SpeakerResult.objects.filter(
                    tournament__season=season,
                    tournament__soty=True,
                    type_of_place=Debater.VARSITY,
                ).select_related("tournament"),
                to_attr="season_replay_results",
            )
        ),
        to_attr="season_reaffs",
    )

    toty_qs = (
        TOTY.objects.filter(season=season)
        .select_related(
            "team",
            "tournament_one",
            "tournament_two",
            "tournament_three",
            "tournament_four",
            "tournament_five",
            "tournament_six",
        )
        .prefetch_related(
            Prefetch(
                "team__debaters",
                queryset=Debater.objects.select_related("school"),
            ),
            team_results_prefetch,
            toty_reaff_prefetch,
        )
        .order_by("-points", "place")
    )

    soty_qs = (
        SOTY.objects.filter(season=season)
        .select_related(
            "debater__school",
            "tournament_one",
            "tournament_two",
            "tournament_three",
            "tournament_four",
            "tournament_five",
            "tournament_six",
        )
        .prefetch_related(speaker_results_prefetch, reaff_prefetch)
        .order_by("-points", "place")
    )

    standings = {"toty": [], "soty": []}
    timeline_dates = set()

    for entry in toty_qs:
        payload = _standing_payload(entry, "team", REPLAY_MARKER_LIMITS["toty"], request)
        markers = _team_replay_markers(entry, season, request)
        payload["all_markers"] = markers
        standings["toty"].append(payload)
        for marker in markers:
            earned_on = marker.get("earned_on")
            if earned_on:
                timeline_dates.add(earned_on)

    for entry in soty_qs:
        payload = _standing_payload(entry, "debater", REPLAY_MARKER_LIMITS["soty"], request)
        markers = _speaker_replay_markers(entry, season, request)
        payload["all_markers"] = markers
        standings["soty"].append(payload)
        for marker in markers:
            earned_on = marker.get("earned_on")
            if earned_on:
                timeline_dates.add(earned_on)

    coty_rows, coty_dates = _collect_coty_replay_standings(season, request)
    standings["coty"] = coty_rows
    timeline_dates.update(coty_dates)

    return standings, sorted(timeline_dates)


def _marker_counts_for_date(marker, through_iso):
    if not marker or not through_iso:
        return False
    earned_on = marker.get("earned_on")
    if not earned_on:
        return True
    return earned_on <= through_iso


def _marker_points(marker):
    value = marker.get("points")
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalize_marker(marker):
    normalized = dict(marker)
    normalized["points"] = round(_marker_points(marker), 4)
    return normalized


def _sort_markers_for_partial(markers):
    return sorted(
        markers,
        key=lambda marker: (
            -_marker_points(marker),
            marker.get("earned_on") or "",
            marker.get("result_id") or 0,
        ),
    )


def _entity_name(entry, entity_attr):
    if entity_attr == "team":
        team = entry.get("team") or {}
        return team.get("name") or ""
    if entity_attr == "debater":
        debater = entry.get("debater") or {}
        return debater.get("name") or ""
    if entity_attr == "school":
        school = entry.get("school") or {}
        return school.get("name") or ""
    return ""


def _assign_places(rows):
    current_place = 1
    last_points = None
    for index, row in enumerate(rows):
        points = row["points"]
        if last_points is not None and abs(points - last_points) < 0.001:
            row["place"] = current_place
            row["tied"] = True
            if index > 0:
                rows[index - 1]["tied"] = True
                rows[index - 1]["place"] = current_place
        else:
            current_place = index + 1
            row["place"] = current_place
            row["tied"] = False
        last_points = points


def _build_partial_rows(entries, through_iso, marker_limit, entity_attr):
    rows = []
    for entry in entries:
        available_markers = [
            marker
            for marker in entry.get("all_markers", [])
            if _marker_counts_for_date(marker, through_iso)
        ]
        if not available_markers:
            continue

        ranked_markers = _sort_markers_for_partial(available_markers)
        if marker_limit:
            ranked_markers = ranked_markers[:marker_limit]
        cleaned_markers = [_normalize_marker(marker) for marker in ranked_markers]
        total_points = round(sum(marker["points"] for marker in cleaned_markers), 4)

        row_payload = {
            "id": entry.get("id"),
            "season": entry.get("season"),
            "season_display": entry.get("season_display"),
            "markers": cleaned_markers,
            "points": total_points,
            "type": entity_attr,
            "original_place": entry.get("place") or 0,
            "_sort_name": _entity_name(entry, entity_attr).lower(),
        }

        if entry.get("team"):
            row_payload["team"] = entry["team"]
        if entry.get("debater"):
            row_payload["debater"] = entry["debater"]
        if entry.get("school"):
            row_payload["school"] = entry["school"]

        rows.append(row_payload)

    rows.sort(
        key=lambda row: (-row["points"], row["original_place"], row["_sort_name"])
    )
    _assign_places(rows)

    for row in rows:
        row["place_display"] = row["place"]
        row.pop("original_place", None)
        row.pop("_sort_name", None)

    return rows


def _qual_display(debater, season):
    quals = debater.quals.filter(season=season, qual_type__gt=0)
    return ", ".join(sorted({qual.get_qual_type_display() for qual in quals}))


def _qual_contribution(points, qualled):
    contribution = points + (6 if qualled else 0)
    return min(66, contribution)


def _coty_breakdown_for_school(school, season, request):
    breakdown = []
    for qual_point in get_qualled_debaters(school, season):
        debater = qual_point.debater
        breakdown.append(
            {
                "debater": serialize_debater(debater, request),
                "points": qual_point.points,
                "qualled": qual_point.qualled,
                "contribution": _qual_contribution(qual_point.points, qual_point.qualled),
                "auto_quals": _qual_display(debater, season),
            }
        )
    return breakdown


def _school_members_for_season(school, season, request):
    members = []
    for debater in get_debaters_for_season(school, season):
        years_on_team = 0
        try:
            years_on_team = int(season) - int(debater.first_season) + 1
        except (TypeError, ValueError):
            years_on_team = 0

        members.append(
            {
                "debater": serialize_debater(debater, request),
                "years_on_team": years_on_team,
            }
        )
    return members


def _visible_videos(videos, request):
    return [
        serialize_video(video, request)
        for video in videos
        if has_perm(request.user, video)
    ]


def _standing_payload(entry, entity_attr, marker_count, request, extra=None, lite=False):
    payload = {
        "season": entry.season,
        "season_display": _format_season_display(entry.season),
        "place_display": place_as_round(entry.place) if entry.place else "",
        "points": entry.points,
        "markers": _serialize_markers(entry, marker_count, request),
    }

    if not lite:
        payload["id"] = entry.id
        payload["place"] = entry.place
        payload["tied"] = entry.tied

    entity = getattr(entry, entity_attr, None)
    if entity_attr == "team" and entity:
        payload["team"] = _lite_team(entity, request)
    elif entity_attr == "debater" and entity:
        payload["debater"] = _lite_debater(entity, request)
        payload["school"] = _lite_school(entity.school, request)
    elif entity_attr == "school" and entity:
        payload["school"] = _lite_school(entity, request)

    if extra:
        payload.update(extra)

    return payload


@extend_schema_view(
    get=extend_schema(
        summary="Season standings",
        description="Fetch TOTY, COTY, SOTY, NOTY, and online qualifier standings for the requested season.",
        parameters=[SEASON_QUERY_PARAM, BOARD_PARAM, LIMIT_PARAM],
        responses=SeasonStandingsResponseSerializer,
    )
)
class SeasonStandingsAPIView(APIView):
    """
    Return TOTY/COTY/SOTY/NOTY/Online standings in a machine-friendly format.
    Requires a season query parameter (defaults to CURRENT_SEASON).
    """

    cache_timeout = 300

    def get(self, request):
        season = _resolve_season(request)
        cache_key = f"api:standings:{season}"
        base_payload = cache.get(cache_key)
        if not base_payload:
            toty_qs = (
                TOTY.objects.filter(season=season)
                .select_related(
                    "team",
                    "tournament_one",
                    "tournament_two",
                    "tournament_three",
                    "tournament_four",
                    "tournament_five",
                    "tournament_six",
                )
                .prefetch_related(
                    Prefetch(
                        "team__debaters",
                        queryset=Debater.objects.select_related("school"),
                    )
                )
                .order_by("-points", "place")
            )

            soty_qs = (
                SOTY.objects.filter(season=season)
                .select_related(
                    "debater__school",
                    "tournament_one",
                    "tournament_two",
                    "tournament_three",
                    "tournament_four",
                    "tournament_five",
                    "tournament_six",
                )
                .order_by("-points", "place")
            )

            coty_qs = (
                COTY.objects.filter(season=season)
                .select_related("school")
                .order_by("-points", "place")
            )

            render_noty = int(season) <= settings.LAST_NOTY_SEASON
            noty_qs = NOTY.objects.none()
            if render_noty:
                noty_qs = (
                    NOTY.objects.filter(season=season)
                    .select_related(
                        "debater__school",
                        "tournament_one",
                        "tournament_two",
                        "tournament_three",
                        "tournament_four",
                        "tournament_five",
                    )
                    .order_by("-points", "place")
                )

            online_qs = OnlineQUAL.objects.none()
            using_online_quals = season in settings.ONLINE_SEASONS
            if using_online_quals:
                online_qs = (
                    OnlineQUAL.objects.filter(season=season)
                    .select_related(
                        "debater__school",
                        "tournament_one",
                        "tournament_two",
                        "tournament_three",
                        "tournament_four",
                        "tournament_five",
                        "tournament_six",
                    )
                    .order_by("-points", "place")
                )

            standings = {
                "toty": [
                    _standing_payload(entry, "team", 5, request, lite=True)
                    for entry in toty_qs
                ],
                "soty": [
                    _standing_payload(entry, "debater", 6, request, lite=True)
                    for entry in soty_qs
                ],
                "coty": [
                    {
                        **_standing_payload(entry, "school", 0, request, lite=True),
                        "breakdown": _coty_breakdown_for_school(
                            entry.school, season, request
                        ),
                    }
                    for entry in coty_qs
                ],
            }

            if render_noty:
                standings["noty"] = [
                    _standing_payload(entry, "debater", 5, request, lite=True)
                    for entry in noty_qs
                ]

            if using_online_quals:
                standings["online_quals"] = [
                    _standing_payload(
                        entry,
                        "debater",
                        6,
                        request,
                        extra={
                            "qualified": entry.points >= settings.ONLINE_QUAL_BAR,
                        },
                        lite=True,
                    )
                    for entry in online_qs
                ]

            base_payload = {
                "season": season,
                "season_display": _format_season_display(season),
                "available_seasons": [
                    {"value": value, "label": label} for value, label in settings.SEASONS
                ],
                "render_noty": render_noty,
                "using_online_quals": using_online_quals,
                "online_qual_bar": settings.ONLINE_QUAL_BAR,
                "standings": standings,
                "links": {
                    "self": _llm_proxy_url(
                        request, f"{reverse('api:season_standings')}?season={season}"
                    ),
                },
            }
            cache.set(cache_key, base_payload, self.cache_timeout)

        selected_boards = _selected_boards(request, list(base_payload["standings"].keys()))
        limit = _parse_limit_param(request)
        filtered_standings = _filter_standings_payload(
            base_payload["standings"], selected_boards, limit
        )

        payload = dict(base_payload)
        payload["standings"] = filtered_standings
        payload["links"] = dict(base_payload.get("links", {}))
        payload["links"]["self"] = _llm_proxy_url(
            request, _current_endpoint_path(request)
        )
        return Response(payload)


@extend_schema_view(
    get=extend_schema(
        summary="Standings replay dataset",
        description="Return TOTY/SOTY/COTY standings plus every tournament marker for replay visualizations.",
        parameters=[SEASON_QUERY_PARAM, BOARD_PARAM, LIMIT_PARAM],
        responses=SeasonStandingsReplayResponseSerializer,
    )
)
class SeasonStandingsReplayAPIView(APIView):
    """
    Return TOTY/SOTY/COTY standings along with all season markers for replay visualizations.
    """

    cache_timeout = 300

    def get(self, request):
        season = _resolve_season(request)
        cache_key = f"api:standings_replay:{season}"
        base_payload = cache.get(cache_key)
        if not base_payload:
            standings, timeline_dates = _collect_replay_standings(season, request)
            payload = {
                "season": season,
                "season_display": _format_season_display(season),
                "available_seasons": [
                    {"value": value, "label": label} for value, label in settings.SEASONS
                ],
                "standings": standings,
                "timeline_dates": timeline_dates,
                "marker_limits": REPLAY_MARKER_LIMITS,
                "links": {
                    "self": request.build_absolute_uri(
                        f"{reverse('api:season_standings_replay')}?season={season}"
                    ),
                    "html": request.build_absolute_uri(
                        f"{reverse('core:standings_replay')}?season={season}"
                    ),
                },
            }
            cache.set(cache_key, payload, self.cache_timeout)
            base_payload = payload

        selected_boards = _selected_boards(request, list(base_payload["standings"].keys()))
        limit = _parse_limit_param(request)
        filtered_standings = _filter_standings_payload(
            base_payload["standings"], selected_boards, limit
        )

        payload = dict(base_payload)
        payload["standings"] = filtered_standings
        payload["links"] = dict(base_payload.get("links", {}))
        payload["links"]["self"] = request.build_absolute_uri(_current_endpoint_path(request))
        return Response(payload)


@extend_schema_view(
    get=extend_schema(
        summary="Standings snapshot through a date",
        description="Compute standings as of the supplied ISO date by reusing replay markers.",
        parameters=[SEASON_QUERY_PARAM, THROUGH_DATE_PARAM, BOARD_PARAM, LIMIT_PARAM],
        responses={200: StandingsThroughDateResponseSerializer, 400: ErrorResponseSerializer},
    )
)
class StandingsThroughDateAPIView(APIView):
    """Return standings snapshots through a supplied ISO date."""

    cache_timeout = 300

    def get(self, request):
        season = _resolve_season(request)
        through_param = request.GET.get("through") or request.GET.get("date")
        if not through_param:
            return Response(
                {"detail": "Provide a through=YYYY-MM-DD query parameter."},
                status=400,
            )

        try:
            through_date = date_class.fromisoformat(through_param)
        except ValueError:
            return Response(
                {"detail": "Invalid through date; use YYYY-MM-DD."}, status=400
            )

        through_iso = through_date.isoformat()
        cache_key = f"api:standings_through_date:{season}:{through_iso}"
        base_payload = cache.get(cache_key)
        if not base_payload:
            standings, _ = _collect_replay_standings(season, request)
            partial = {
                "toty": _build_partial_rows(
                    standings.get("toty", []),
                    through_iso,
                    REPLAY_MARKER_LIMITS["toty"],
                    "team",
                ),
                "soty": _build_partial_rows(
                    standings.get("soty", []),
                    through_iso,
                    REPLAY_MARKER_LIMITS["soty"],
                    "debater",
                ),
                "coty": _build_partial_rows(
                    standings.get("coty", []),
                    through_iso,
                    REPLAY_MARKER_LIMITS["coty"],
                    "school",
                ),
            }

            base_payload = {
                "season": season,
                "season_display": _format_season_display(season),
                "through": through_iso,
                "marker_limits": REPLAY_MARKER_LIMITS,
                "standings": partial,
                "links": {
                    "self": request.build_absolute_uri(
                        f"{reverse('api:standings_through_date')}?season={season}&through={through_iso}"
                    ),
                    "replay_api": request.build_absolute_uri(
                        f"{reverse('api:season_standings_replay')}?season={season}"
                    ),
                    "replay_html": request.build_absolute_uri(
                        f"{reverse('core:standings_replay')}?season={season}"
                    ),
                    "full_standings": request.build_absolute_uri(
                        f"{reverse('api:season_standings')}?season={season}"
                    ),
                },
            }
            cache.set(cache_key, base_payload, self.cache_timeout)

        selected_boards = _selected_boards(request, list(base_payload["standings"].keys()))
        limit = _parse_limit_param(request)
        filtered = _filter_standings_payload(
            base_payload["standings"], selected_boards, limit
        )

        payload = dict(base_payload)
        payload["standings"] = filtered
        payload["links"] = dict(base_payload.get("links", {}))
        payload["links"]["self"] = request.build_absolute_uri(_current_endpoint_path(request))
        return Response(payload)


@extend_schema_view(
    get=extend_schema(
        summary="OTY scoring guide",
        description="Return the Markdown guide describing TOTY/SOTY/COTY scoring rules.",
        responses={200: OTYGuideResponseSerializer, 503: ErrorResponseSerializer},
    )
)
class OTYGuideAPIView(APIView):
    """Expose the OTY scoring explainer in a machine-readable format."""

    cache_timeout = 300

    def get(self, request):
        cache_key = "api:oty_guide"
        base_payload = cache.get(cache_key)
        if not base_payload:
            body = _load_oty_guide_text()
            if not body:
                return Response(
                    {"detail": "OTY guide is temporarily unavailable."}, status=503
                )

            base_payload = {
                "title": "APDA OTY Scoring Guide",
                "season": settings.CURRENT_SEASON,
                "season_display": _format_season_display(settings.CURRENT_SEASON),
                "format": "markdown",
                "body": body,
                "last_modified": _oty_guide_last_modified(),
                "links": {
                    "plain_text": request.build_absolute_uri(
                        reverse("llm_oty_explainer")
                    ),
                    "llm_proxy": _llm_proxy_url(
                        request, reverse("api:oty_guide")
                    ),
                },
            }

            cache.set(cache_key, base_payload, self.cache_timeout)

        payload = dict(base_payload)
        payload["links"] = dict(base_payload.get("links", {}))
        payload["links"]["self"] = _llm_proxy_url(request, _current_endpoint_path(request))
        return Response(payload)


@extend_schema_view(
    get=extend_schema(
        summary="Tournament schedule",
        description="Return the public tournament schedule grouped by month/week with OTY notes.",
        parameters=[SEASON_QUERY_PARAM],
        responses=ScheduleResponseSerializer,
    )
)
class ScheduleAPIView(APIView):
    """Expose the APDA tournament schedule grouped by month/week."""

    cache_timeout = 300

    def get(self, request):
        season = _resolve_season(request)
        cache_key = f"api:schedule:{season}"
        cached = cache.get(cache_key)
        if cached:
            return Response(cached)

        tournaments = (
            Tournament.objects.filter(season=season)
            .select_related("host")
            .order_by("date")
        )

        months = _schedule_month_blocks(tournaments, request)

        payload = {
            "season": season,
            "season_display": _format_season_display(season),
            "notes": [
                "COTY points and qual points describe the same scoring buckets.",
                "Most schools abstain from competing at tournaments they host, but this rarely affects yearlong awards.",
            ],
            "months": months,
            "links": {
                "self": _llm_proxy_url(request, _current_endpoint_path(request)),
            },
        }

        cache.set(cache_key, payload, self.cache_timeout)
        return Response(payload)


def _determine_team_for_debater_tournament(debater, bucket):
    if bucket["team_results"]:
        return bucket["team_results"][0].team

    for rnd in bucket["rounds"]:
        if rnd.gov.debaters.filter(pk=debater.pk).exists():
            return rnd.gov
        if rnd.opp.debaters.filter(pk=debater.pk).exists():
            return rnd.opp
    return None


def _build_debater_season_results(debater, request):
    tournament_bucket = {}

    team_results = (
        TeamResult.objects.filter(team__debaters=debater)
        .select_related("tournament", "team")
        .order_by("tournament__date")
    )
    for result in team_results:
        bucket = tournament_bucket.setdefault(
            result.tournament_id,
            {"tournament": result.tournament, "team_results": [], "speaker_results": [], "rounds": []},
        )
        bucket["team_results"].append(result)

    speaker_results = (
        SpeakerResult.objects.filter(debater=debater)
        .select_related("tournament")
        .order_by("tournament__date")
    )
    for result in speaker_results:
        bucket = tournament_bucket.setdefault(
            result.tournament_id,
            {"tournament": result.tournament, "team_results": [], "speaker_results": [], "rounds": []},
        )
        bucket["speaker_results"].append(result)

    rounds = (
        Round.objects.filter(Q(gov__debaters=debater) | Q(opp__debaters=debater))
        .select_related("tournament", "gov", "opp")
        .order_by("tournament__date", "round_number")
    )
    for rnd in rounds:
        bucket = tournament_bucket.setdefault(
            rnd.tournament_id,
            {"tournament": rnd.tournament, "team_results": [], "speaker_results": [], "rounds": []},
        )
        bucket["rounds"].append(rnd)

    season_index = defaultdict(list)
    for bucket in tournament_bucket.values():
        tournament = bucket["tournament"]
        team = _determine_team_for_debater_tournament(debater, bucket)
        record = get_record(tournament, team) if team else ""
        tab_card = _serialize_tab_card(team, tournament, request) if team else []

        team_results_payload = sorted(
            bucket["team_results"],
            key=lambda res: (-res.type_of_place, res.place),
        )
        speaker_results_payload = sorted(
            bucket["speaker_results"],
            key=lambda res: (-res.type_of_place, res.place),
        )

        payload = {
            "tournament": serialize_tournament(tournament, request),
            "team": serialize_team(team, request) if team else None,
            "team_results": [
                serialize_team_result(res, request) for res in team_results_payload
            ],
            "speaker_results": [
                serialize_speaker_result(res, request) for res in speaker_results_payload
            ],
            "record": record,
            "tab_card": tab_card,
        }
        season_index[tournament.season].append(
            (tournament.date or date_class.min, payload)
        )

    season_summaries = []
    for season, entries in season_index.items():
        sorted_entries = [
            entry for _, entry in sorted(entries, key=lambda item: item[0])
        ]
        season_summaries.append(
            {
                "season": season,
                "season_display": _format_season_display(season),
                "tournaments": sorted_entries,
            }
        )

    season_summaries.sort(key=lambda block: block["season"], reverse=True)
    return season_summaries


def _build_team_tournaments(team, request):
    entries = []
    tournaments_handled = set()

    team_results = team.team_results.select_related("tournament").order_by(
        "tournament__date"
    )
    for result in team_results:
        tournament = result.tournament
        entries.append(
            {
                "kind": "award",
                "team_result": serialize_team_result(result, request),
                "record": get_record(tournament, team),
                "tab_card": _serialize_tab_card(team, tournament, request),
                "tournament": serialize_tournament(tournament, request),
            }
        )
        tournaments_handled.add(tournament.id)

    extra_rounds = (
        Round.objects.filter(Q(gov=team) | Q(opp=team))
        .select_related("tournament")
        .order_by("tournament__date")
    )
    for rnd in extra_rounds:
        tournament = rnd.tournament
        if tournament.id in tournaments_handled:
            continue

        entries.append(
            {
                "kind": "participation",
                "team_result": None,
                "record": get_record(tournament, team),
                "tab_card": _serialize_tab_card(team, tournament, request),
                "tournament": serialize_tournament(tournament, request),
            }
        )
        tournaments_handled.add(tournament.id)

    entries.sort(
        key=lambda item: (
            item["tournament"]["date"] or "",
            item["tournament"]["id"],
        )
    )
    return entries


def _adjust_speaker_results(queryset, request, varsity=True):
    results = list(queryset)
    payload = []
    total = len(results)

    for idx, result in enumerate(results):
        place = result.place
        tie_flag = result.tie

        if varsity:
            if result.tie:
                place -= 1
            if idx < total - 1 and results[idx + 1].tie:
                tie_flag = True
        else:
            if idx < total - 1 and results[idx + 1].tie:
                tie_flag = True
            if tie_flag:
                place -= 1

        data = serialize_speaker_result(result, request)
        data["display_place"] = place
        data["tie_display"] = tie_flag
        payload.append(data)

    return payload


@extend_schema_view(
    get=extend_schema(
        summary="Team detail",
        parameters=[TEAM_ID_PARAM, ENTRY_LIMIT_PARAM],
        responses={200: TeamDetailResponseSerializer, 404: ErrorResponseSerializer},
    )
)
class TeamDetailAPIView(APIView):
    """
    Expose a team's roster, TOTY history, and tournament performances.
    """

    def get(self, request, pk):
        try:
            team = (
                Team.objects.prefetch_related(
                    Prefetch(
                        "debaters",
                        queryset=Debater.objects.select_related("school"),
                    )
                ).get(pk=pk)
            )
        except Team.DoesNotExist as exc:
            raise Http404("Team not found") from exc

        tournaments = _build_team_tournaments(team, request)
        toty_history = [
            _standing_payload(entry, "team", 5, request)
            for entry in team.toty.select_related(
                "tournament_one",
                "tournament_two",
                "tournament_three",
                "tournament_four",
                "tournament_five",
            ).order_by("-points", "season")
        ]

        entry_limit = _parse_limit_param(request)
        if entry_limit:
            tournaments = _trim_entries(tournaments, entry_limit)
            toty_history = _trim_entries(toty_history, entry_limit)

        payload = {
            "team": serialize_team(team, request),
            "toty_history": toty_history,
            "tournaments": tournaments,
            "links": {
                "self": _llm_proxy_url(request, _current_endpoint_path(request)),
            },
        }

        return Response(payload)


@extend_schema_view(
    get=extend_schema(
        summary="Tournament detail",
        parameters=[TOURNAMENT_ID_PARAM],
        responses={200: TournamentDetailResponseSerializer, 404: ErrorResponseSerializer},
    )
)
class TournamentDetailAPIView(APIView):
    """
    Provide tournament metadata, awards, speaker results, tab cards, and videos.
    """

    def get(self, request, pk):
        try:
            tournament = (
                Tournament.objects.select_related("host")
                .get(pk=pk)
            )
        except Tournament.DoesNotExist as exc:
            raise Http404("Tournament not found") from exc

        varsity_team_results = [
            serialize_team_result(result, request)
            for result in tournament.team_results.filter(
                type_of_place=Debater.VARSITY, place__gt=0
            ).order_by("place")
        ]
        novice_team_results = [
            serialize_team_result(result, request)
            for result in tournament.team_results.filter(
                type_of_place=Debater.NOVICE, place__gt=0
            )
            .exclude(
                team__in=tournament.team_results.filter(
                    type_of_place=Debater.VARSITY, place__gt=0
                ).values_list("team", flat=True)
            )
            .order_by("place")
        ]

        varsity_speakers = _adjust_speaker_results(
            tournament.speaker_results.filter(type_of_place=Debater.VARSITY).order_by("place"),
            request,
            varsity=True,
        )
        novice_speakers = _adjust_speaker_results(
            tournament.speaker_results.filter(type_of_place=Debater.NOVICE).order_by("place"),
            request,
            varsity=False,
        )

        tab_cards_available = Round.objects.filter(tournament=tournament).exists()
        teams_for_tab_cards = []
        if tab_cards_available:
            teams = (
                Team.objects.filter(
                    Q(govs__tournament=tournament) | Q(opps__tournament=tournament)
                )
                .distinct()
                .prefetch_related(
                    Prefetch(
                        "debaters",
                        queryset=Debater.objects.select_related("school"),
                    )
                )
            )
            teams_for_tab_cards = [
                {
                    "team": serialize_team(team, request),
                    "tab_card": _serialize_tab_card(team, tournament, request),
                }
                for team in teams
            ]

        videos = (
            tournament.videos.select_related("pm", "mg", "lo", "mo", "tournament")
            .prefetch_related("tags")
        )

        payload = {
            "tournament": serialize_tournament(tournament, request),
            "team_awards": {
                "varsity": varsity_team_results,
                "novice": novice_team_results,
            },
            "speaker_awards": {
                "varsity": varsity_speakers,
                "novice": novice_speakers,
            },
            "tab_cards_available": tab_cards_available,
            "team_tab_cards": teams_for_tab_cards if request.user.is_authenticated else [],
            "videos": _visible_videos(videos, request),
            "links": {
                "self": _llm_proxy_url(request, _current_endpoint_path(request)),
            },
        }

        return Response(payload)


@extend_schema_view(
    get=extend_schema(
        summary="School detail",
        parameters=[SCHOOL_PK_PARAM, SEASON_QUERY_PARAM, ENTRY_LIMIT_PARAM],
        responses={200: SchoolDetailResponseSerializer, 404: ErrorResponseSerializer},
    )
)
class SchoolDetailAPIView(APIView):
    """
    Season-aware school profile with COTY history, hosted tournaments, and roster info.
    """

    def get(self, request, pk):
        try:
            school = School.objects.get(pk=pk)
        except School.DoesNotExist as exc:
            raise Http404("School not found") from exc

        season = _resolve_season(request)

        coty_history = [
            _standing_payload(entry, "school", 0, request)
            for entry in school.coty.order_by("-season")
        ]

        tournaments = [
            serialize_tournament(tournament, request)
            for tournament in school.hosted_tournaments.order_by("-date")
        ]

        season_summary = {
            "season": season,
            "season_display": _format_season_display(season),
            "members": _school_members_for_season(school, season, request),
            "coty_breakdown": _coty_breakdown_for_school(school, season, request),
        }

        entry_limit = _parse_limit_param(request)
        if entry_limit:
            coty_history = _trim_entries(coty_history, entry_limit)
            tournaments = _trim_entries(tournaments, entry_limit)

        payload = {
            "school": serialize_school(school, request),
            "season": season,
            "season_display": _format_season_display(season),
            "available_seasons": [
                {"value": value, "label": label} for value, label in settings.SEASONS
            ],
            "coty_history": coty_history,
            "hosted_tournaments": tournaments,
            "season_summary": season_summary,
            "links": {
                "self": _llm_proxy_url(request, _current_endpoint_path(request)),
            },
        }

        return Response(payload)


@extend_schema_view(
    get=extend_schema(
        summary="Debater detail",
        parameters=[DEBATER_ID_PARAM, ENTRY_LIMIT_PARAM],
        responses={200: DebaterDetailResponseSerializer, 404: ErrorResponseSerializer},
    )
)
class DebaterDetailAPIView(APIView):
    """
    Return a debater's public profile, standings history, and tournament results.
    """

    def get(self, request, pk):
        try:
            debater = (
                Debater.objects.select_related("school", "alias_group")
                .get(pk=pk)
            )
        except Debater.DoesNotExist as exc:
            raise Http404("Debater not found") from exc

        season_results = _build_debater_season_results(debater, request)

        toty_history = [
            _standing_payload(entry, "team", 5, request)
            for entry in TOTY.objects.filter(team__debaters=debater)
            .select_related(
                "team",
                "tournament_one",
                "tournament_two",
                "tournament_three",
                "tournament_four",
                "tournament_five",
            )
            .prefetch_related(
                Prefetch(
                    "team__debaters",
                    queryset=Debater.objects.select_related("school"),
                )
            )
            .order_by("place", "season")
        ]

        soty_history = [
            _standing_payload(entry, "debater", 6, request)
            for entry in debater.soty.select_related(
                "tournament_one",
                "tournament_two",
                "tournament_three",
                "tournament_four",
                "tournament_five",
                "tournament_six",
            ).order_by("place", "season")
        ]

        noty_history = [
            _standing_payload(entry, "debater", 5, request)
            for entry in debater.noty.select_related(
                "tournament_one",
                "tournament_two",
                "tournament_three",
                "tournament_four",
                "tournament_five",
            ).order_by("place", "season")
        ]

        alias_history = []
        if debater.alias_group:
            alias_history = [
                serialize_debater(alias, request)
                for alias in debater.alias_group.debaters.exclude(pk=debater.pk)
                .select_related("school")
                .order_by("school__name", "first_name", "last_name")
            ]

        teams = (
            Team.objects.filter(debaters=debater)
            .annotate(tournament_count=Count("team_results__tournament", distinct=True))
            .prefetch_related(
                Prefetch(
                    "debaters",
                    queryset=Debater.objects.select_related("school"),
                )
            )
        )
        teams_payload = [
            {
                "team": serialize_team(team, request),
                "tournament_count": team.tournament_count,
                "toty_points": team.toty_points,
            }
            for team in teams
        ]
        teams_payload.sort(
            key=lambda item: (
                -item["tournament_count"],
                -item["toty_points"],
                item["team"]["id"],
            )
        )

        videos_queryset = (
            Video.objects.filter(
                Q(pm=debater) | Q(mg=debater) | Q(lo=debater) | Q(mo=debater)
            )
            .select_related("tournament", "pm", "mg", "lo", "mo")
            .prefetch_related("tags")
            .distinct()
        )

        can_view_paradigm = (
            debater.paradigm
            and request.user.is_authenticated
            and getattr(request.user, "can_view_private_videos", False)
        )

        entry_limit = _parse_limit_param(request)
        if entry_limit:
            season_results = _trim_entries(season_results, entry_limit)
            teams_payload = _trim_entries(teams_payload, entry_limit)
            toty_history = _trim_entries(toty_history, entry_limit)
            soty_history = _trim_entries(soty_history, entry_limit)
            noty_history = _trim_entries(noty_history, entry_limit)

        contact_preferences = {
            "paradigm_url": debater.paradigm if can_view_paradigm else None,
            "to_outreach": debater.dino_to_contact_opt_in,
            "judge_outreach": debater.dino_judge_contact_opt_in and debater.is_dino,
        }
        if debater.show_region:
            label_lookup = dict(Debater.REGION_CHOICES)
            contact_preferences["regions"] = [
                {
                    "value": code,
                    "label": label_lookup.get(code, code),
                }
                for code in debater.region_list
            ]

        payload = {
            "debater": serialize_debater(debater, request),
            "first_season": debater.first_season,
            "latest_season": debater.latest_season,
            "is_dino": debater.is_dino,
            "contact_preferences": contact_preferences,
            "also_debated_under": alias_history,
            "teams": teams_payload,
            "standings": {
                "toty": toty_history,
                "soty": soty_history,
                "noty": noty_history,
            },
            "season_summaries": season_results,
            "videos": _visible_videos(videos_queryset, request),
            "links": {
                "self": _llm_proxy_url(request, _current_endpoint_path(request)),
            },
        }

        return Response(payload)


class LLMProxyView(View):
    """
    Quick and dirty proxy view to help LLM browsing tools read JSON API responses.
    
    GET /llm?endpoint=/api/standings
    
    Takes an API endpoint path and returns the JSON response wrapped in HTML
    with a <pre> tag for better readability by LLM tools like ChatGPT's browser.
    
    Security: Only allows internal API paths (starting with /) to prevent SSRF attacks.
    """
    
    def get(self, request):
        # Get the endpoint parameter
        endpoint = request.GET.get('endpoint', '')
        
        # Security: Validate that endpoint is provided and starts with /
        if not endpoint:
            return HttpResponse(
                '<!DOCTYPE html><html><head><title>Error</title></head><body>'
                '<h1>Error: Missing endpoint parameter</h1>'
                '<p>Usage: /llm?endpoint=/api/standings</p>'
                '</body></html>',
                content_type='text/html',
                status=400
            )
        
        # Security: Only allow relative paths starting with / to prevent SSRF
        if not endpoint.startswith('/'):
            return HttpResponse(
                '<!DOCTYPE html><html><head><title>Error</title></head><body>'
                '<h1>Error: Invalid endpoint</h1>'
                '<p>Endpoint must start with / (relative path only)</p>'
                '<p>Example: /llm?endpoint=/api/standings</p>'
                '</body></html>',
                content_type='text/html',
                status=400
            )
        
        # Additional security: Only allow whitelisted paths (/api/ and /.well-known/)
        allowed_prefix = any(endpoint.startswith(prefix) for prefix in LLM_PROXY_ALLOWED_PREFIXES)
        allowed_path = endpoint in LLM_PROXY_ALLOWED_PATHS
        if not allowed_prefix and not allowed_path:
            return HttpResponse(
                '<!DOCTYPE html><html><head><title>Error</title></head><body>'
                '<h1>Error: Invalid endpoint</h1>'
                '<p>Only /api/ and whitelisted /.well-known/ endpoints are allowed</p>'
                '<p>Example: /llm?endpoint=/api/standings or /llm?endpoint=/.well-known/openapi.json</p>'
                '</body></html>',
                content_type='text/html',
                status=400
            )
        
        # Security: Prevent path traversal attacks
        if '..' in endpoint or '//' in endpoint:
            return HttpResponse(
                '<!DOCTYPE html><html><head><title>Error</title></head><body>'
                '<h1>Error: Invalid endpoint</h1>'
                '<p>Path traversal patterns are not allowed</p>'
                '</body></html>',
                content_type='text/html',
                status=400
            )
        
        # Make an internal request to the endpoint
        factory = RequestFactory()
        secure = request.is_secure()
        host = request.get_host()
        factory_kwargs = {"HTTP_HOST": host}
        
        # Preserve query parameters from the original endpoint if any
        if '?' in endpoint:
            path, query = endpoint.split('?', 1)
            # Parse query parameters safely with URL decoding
            query_params = {}
            for item in query.split('&'):
                if '=' in item:
                    key, value = item.split('=', 1)
                    # Decode URL-encoded parameters
                    query_params[unquote_plus(key)] = unquote_plus(value)
                else:
                    # Handle parameters without values (e.g., ?flag)
                    query_params[unquote_plus(item)] = ''
            internal_request = factory.get(path, data=query_params, secure=secure, **factory_kwargs)
        else:
            internal_request = factory.get(endpoint, secure=secure, **factory_kwargs)
        
        # Copy user and session from the original request
        internal_request.user = request.user
        internal_request.session = request.session
        
        try:
            # Resolve the URL and call the view
            resolved = resolve(endpoint.split('?')[0])
            response = resolved.func(internal_request, *resolved.args, **resolved.kwargs)
            
            # Check if response is successful
            if response.status_code != 200:
                LOGGER.warning(
                    "LLM proxy received status %s for endpoint %s",
                    response.status_code,
                    endpoint,
                )
                escaped_endpoint = html.escape(endpoint)
                return HttpResponse(
                    f'<!DOCTYPE html><html><head><title>Unable to fetch endpoint</title></head><body>'
                    f'<h1>Unable to fetch endpoint</h1>'
                    f'<p>The API returned status {response.status_code} while requesting {escaped_endpoint}. '
                    f'Please verify the path or try again later.</p>'
                    f'</body></html>',
                    content_type='text/html',
                    status=response.status_code
                )
            
            # Parse the JSON response
            json_data = _extract_json_payload(response)
            
            # Pretty-print the JSON with indent=2
            pretty_json = json.dumps(json_data, indent=2, ensure_ascii=False)
            
            # Escape HTML to prevent XSS
            escaped_endpoint = html.escape(endpoint)
            escaped_json = html.escape(pretty_json)
            
            # Return HTML with JSON in a <pre> tag
            html_content = f'''<!DOCTYPE html>
<html>
<head>
    <title>API Response: {escaped_endpoint}</title>
    <meta charset="utf-8">
</head>
<body>
<pre>{escaped_json}</pre>
</body>
</html>'''
            
            return HttpResponse(html_content, content_type='text/html')
            
        except Resolver404:
            escaped_endpoint = html.escape(endpoint)
            return HttpResponse(
                f'<!DOCTYPE html><html><head><title>Unable to fetch endpoint</title></head><body>'
                f'<h1>Unable to fetch endpoint</h1>'
                f'<p>The path {escaped_endpoint} could not be found.</p>'
                f'</body></html>',
                content_type='text/html',
                status=404
            )
        except LLMProxyPayloadError as exc:
            LOGGER.warning(
                "LLM proxy received a non-JSON success response for %s: %s",
                endpoint,
                exc,
            )
            escaped_endpoint = html.escape(endpoint)
            escaped_reason = html.escape(str(exc))
            return HttpResponse(
                f'<!DOCTYPE html><html><head><title>Unable to fetch endpoint</title></head><body>'
                f'<h1>Unable to fetch endpoint</h1>'
                f'<p>The endpoint {escaped_endpoint} returned a successful response, but it was not JSON.</p>'
                f'<p>{escaped_reason}</p>'
                f'</body></html>',
                content_type='text/html',
                status=502
            )
        except Exception:
            LOGGER.exception("LLM proxy encountered an unexpected error for %s", endpoint)
            escaped_endpoint = html.escape(endpoint)
            return HttpResponse(
                f'<!DOCTYPE html><html><head><title>Unable to fetch endpoint</title></head><body>'
                f'<h1>Unable to fetch endpoint</h1>'
                f'<p>An unexpected error occurred while fetching {escaped_endpoint}. '
                f'Please try again later.</p>'
                f'</body></html>',
                content_type='text/html',
                status=500
            )


class LLMDocumentationView(View):
    """Provide plain-text documentation for LLM-friendly endpoints."""

    documentation = """LLM Documentation for black-rod

This public API exposes standings, school rosters, debater profiles, tournaments,
and other APDA Online resources in JSON for third-party tools.

Endpoints:
1. /llm
   - Wrap any /api/ JSON response in HTML for browser-based LLM tools.
   - Usage: /llm?endpoint=/api/<path> (example: /llm?endpoint=/api/standings/?season=2024)
   - Only relative /api/ paths plus /.well-known/* OpenAPI/manifest endpoints are accepted; query parameters are forwarded.

2. /llms.txt
   - This document. Lists machine-friendly interfaces for LLM access.

3. /llm?endpoint=/api/schedule/
   - Machine-readable version of the public tournament schedule grouped the same way as the HTML page.

4. /llm?endpoint=/api/oty-guide/
   - Markdown version of the TOTY/SOTY/COTY scoring guide.

5. /llm/oty-guide/
   - Plain-text explainer covering how TOTY, SOTY, and COTY point races work (including scoring formulas).

Filtering:
- /api/standings/, /api/standings/replay/, and /api/standings/through-date/ accept ?board=<name>&limit=<n> to restrict the boards returned and cap their lengths.
- Detail endpoints such as /api/teams/<id>/detail/, /api/debaters/<id>/detail/, and /api/schools/<id>/detail/ accept ?limit=<n> to truncate repeated lists.

OpenAPI schema:
- /.well-known/openapi.json (OpenAPI 3.1 schema describing every public endpoint)
- /.well-known/ai-plugin.json (ChatGPT plugin manifest that references the schema)

Guidelines:
- Respect caching headers from API responses.
- Avoid crawling authenticated or non-/api/ paths through /llm.
- Report issues to info@apda.online.
"""

    def get(self, request):
        return HttpResponse(self.documentation, content_type="text/plain; charset=utf-8")


class RobotsTxtView(View):
    """Serve a permissive robots.txt that allows all crawlers."""

    content = "User-agent: *\nAllow: /"

    def get(self, request):
        return HttpResponse(self.content, content_type="text/plain; charset=utf-8")


class LLMOTYExplanationView(View):
    """Explain OTY scoring rules for LLM tools."""

    def get(self, request):
        body = _load_oty_guide_text()
        if not body:
            return HttpResponse(
                "OTY guide is temporarily unavailable.",
                content_type="text/plain; charset=utf-8",
                status=503,
            )
        return HttpResponse(body, content_type="text/plain; charset=utf-8")
