from collections import defaultdict
from datetime import date as date_class, timedelta
import html
import json
import logging
from urllib.parse import unquote_plus

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Prefetch, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.test import RequestFactory
from django.urls import resolve, reverse, Resolver404
from django.views import View

from core.models.debater import Debater
from core.models.results.speaker import SpeakerResult
from core.models.results.team import TeamResult
from core.models.round import Round
from core.models.school import School
from core.models.standings.coty import COTY
from core.models.standings.noty import NOTY
from core.models.standings.online_qual import OnlineQUAL
from core.models.standings.soty import SOTY
from core.models.standings.toty import TOTY
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


class ActiveSchoolListAPIView(View):
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
            return JsonResponse(cached_data)

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

        return JsonResponse(data)


class AllSchoolListAPIView(View):
    """
    API endpoint to list all schools.

    GET /api/schools/all/
    Returns: List of all schools in the database
    Cached for 5 minutes.
    """

    def get(self, request):
        cache_key = 'api:all_schools'
        cached_data = cache.get(cache_key)

        if cached_data is not None:
            return JsonResponse(cached_data)

        schools = School.objects.all().order_by('name')

        data = {
            "count": schools.count(),
            "schools": [serialize_school(school, request) for school in schools]
        }
        cache.set(cache_key, data, 300)

        return JsonResponse(data)


class SchoolDebatersAPIView(View):
    """
    API endpoint to list debaters from a specific school.

    GET /api/debaters/<school_id>/
    Returns: List of debaters active in the last 5 years for the specified school
    Cached for 5 minutes per school.
    """

    def get(self, request, school_id):
        cache_key = f'api:school_debaters:{school_id}'
        cached_data = cache.get(cache_key)

        if cached_data is not None:
            return JsonResponse(cached_data)

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
        ).select_related('school').order_by('last_name', 'first_name')

        data = {
            "school": serialize_school(school, request),
            "count": debaters.count(),
            "debaters": [serialize_debater(debater, request) for debater in debaters]
        }

        cache.set(cache_key, data, 300)

        return JsonResponse(data)


# ----------------------------------------------------------------------
# Helper functions shared by multiple AI-friendly endpoints
# ----------------------------------------------------------------------

MARKER_LABELS = ["one", "two", "three", "four", "five", "six"]
LLM_PROXY_ALLOWED_PREFIXES = ("/api/", "/.well-known/")
LLM_PROXY_ALLOWED_PATHS = {"/ai-plugin.json", "/openapi.json"}
LOGGER = logging.getLogger(__name__)


def _available_season_values():
    return [season[0] for season in settings.SEASONS]


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
        "speaks": float(stat.speaks),
        "ranks": float(stat.ranks),
    }


def _absolute_round_url(round_obj, request):
    if not request:
        return round_obj.get_absolute_url()
    return request.build_absolute_uri(round_obj.get_absolute_url())


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
                "round_url": _absolute_round_url(round_obj, request),
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
        "date": tournament.date.isoformat() if tournament.date else None,
    }
    if request:
        data["url"] = request.build_absolute_uri(tournament.get_absolute_url())
    return data


def _tournament_special_notes(tournament):
    name = (tournament.name or "").lower()
    notes = []

    if tournament.qual_type == Tournament.EXPANSION or "bp" in name:
        notes.append(
            "British Parliamentary weekends award autoqual slots, but no TOTY/SOTY/COTY points."
        )
    if tournament.qual_type == Tournament.NATIONALS or "nationals" in name:
        notes.append(
            "Nationals is championship-only: it does not award season points, only a title and autoqual bids."
        )
    if tournament.qual_type == Tournament.GENDER_MINORITY or "gender minority" in name or "gm" in name.split():
        notes.append(
            "Gender Minority events award COTY/qual points only and are invitationals."
        )
    if "bipoc" in name:
        notes.append(
            "BIPOC invitational weekends award COTY (qual) points only."
        )
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


class SeasonStandingsAPIView(View):
    """
    Return TOTY/COTY/SOTY/NOTY/Online standings in a machine-friendly format.
    Requires a season query parameter (defaults to CURRENT_SEASON).
    """

    cache_timeout = 300

    def get(self, request):
        season = _resolve_season(request)
        cache_key = f"api:standings:{season}"
        cached = cache.get(cache_key)
        if cached:
            return JsonResponse(cached)

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

        links = {
            "self": request.build_absolute_uri(
                f"{reverse('api:season_standings')}?season={season}"
            ),
            "html": request.build_absolute_uri(
                f"{reverse('core:index')}?season={season}"
            ),
        }

        payload = {
            "season": season,
            "season_display": _format_season_display(season),
            "available_seasons": [
                {"value": value, "label": label} for value, label in settings.SEASONS
            ],
            "render_noty": render_noty,
            "using_online_quals": using_online_quals,
            "online_qual_bar": settings.ONLINE_QUAL_BAR,
            "standings": standings,
            "links": links,
        }

        cache.set(cache_key, payload, self.cache_timeout)
        return JsonResponse(payload)


class ScheduleAPIView(View):
    """Expose the APDA tournament schedule grouped by month/week."""

    cache_timeout = 300

    def get(self, request):
        season = _resolve_season(request)
        cache_key = f"api:schedule:{season}"
        cached = cache.get(cache_key)
        if cached:
            return JsonResponse(cached)

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
                "Most schools abstain from competing at tournaments they host, but this rarely effects yearlong awards.",
            ],
            "months": months,
            "links": {
                "self": request.build_absolute_uri(
                    f"{reverse('api:schedule')}?season={season}"
                ),
                "html": request.build_absolute_uri(
                    f"{reverse('core:schedule_view')}?season={season}"
                ),
            },
        }

        cache.set(cache_key, payload, self.cache_timeout)
        return JsonResponse(payload)


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


class TeamDetailAPIView(View):
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

        payload = {
            "team": serialize_team(team, request),
            "toty_history": toty_history,
            "tournaments": tournaments,
            "links": {
                "self": request.build_absolute_uri(
                    reverse("api:team_detail", args=[team.id])
                ),
                "html": request.build_absolute_uri(team.get_absolute_url()),
            },
        }

        return JsonResponse(payload)


class TournamentDetailAPIView(View):
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
                "self": request.build_absolute_uri(
                    reverse("api:tournament_detail", args=[tournament.id])
                ),
                "html": request.build_absolute_uri(tournament.get_absolute_url()),
            },
        }

        return JsonResponse(payload)


class SchoolDetailAPIView(View):
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
                "self": request.build_absolute_uri(
                    f"{reverse('api:school_detail', args=[school.id])}?season={season}"
                ),
                "html": request.build_absolute_uri(
                    f"{school.get_absolute_url()}?season={season}"
                ),
            },
        }

        return JsonResponse(payload)


class DebaterDetailAPIView(View):
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

        payload = {
            "debater": serialize_debater(debater, request),
            "first_season": debater.first_season,
            "latest_season": debater.latest_season,
            "is_dino": debater.is_dino,
            "contact_preferences": {
                "paradigm_url": debater.paradigm if can_view_paradigm else None,
                "to_outreach": debater.dino_to_contact_opt_in,
                "judge_outreach": debater.dino_judge_contact_opt_in and debater.is_dino,
            },
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
                "self": request.build_absolute_uri(
                    reverse("api:debater_detail", args=[debater.id])
                ),
                "html": request.build_absolute_uri(debater.get_absolute_url()),
            },
        }

        return JsonResponse(payload)


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
            json_data = json.loads(response.content)
            
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

3. /api/schedule/
   - Machine-readable version of the public tournament schedule grouped the same way as the HTML page.

4. /llm/oty-guide/
   - Plain-text explainer covering how TOTY, SOTY, and COTY point races work (including scoring formulas).

OpenAPI schema:
- /.well-known/openapi.json (OpenAPI 3.0 schema describing every public endpoint)
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

    body = """APDA OTY Scoring Guide

Tournament points (base value)
- Every varsity tournament has a "point" value which grows as more teams attend the tournament. The value is equivalent to the TOTY points received by the tournament winning team of two, and SOTY points earned by the tournamen's top speaker. All tournaments must have at least 8 teams to be worth points. From 8–15 teams, the tournament is worth 8 points. From 16–80 teams the base becomes 12 + floor((teams-16)/8), which continues until 80 teams, when the tournament reaches the maximum 20 points. All other placements and speaker awards are defined relative to that base.

Team of the Year (TOTY)
- Champion = 100% of the base, finalist = base - 4, semifinalists = roughly base - 9 (specifically 3 + 0.75*floor((teams-16)/8) for the 16–71 band), and quarterfinalists get one quarter of the base. At the largest bands (72–79 and 80+) the explicit tables are 19/15/8.25/3.5/0.75 and 20/16/9/4/1.5 respectively. Teams keep only their five best tournament markers and teams are ranked by the sum of those 5 markers. Hybrid partnerships (debaters from different schools) can win events, but their points only flow into SOTY/COTY—not TOTY.

Speaker of the Year (SOTY)
- The same base value applies to speakers. First speaker receives the base; every subsequent speaker drops by 2.5 points (2nd = base-2.5, 3rd = base-5, etc.) until the number would hit zero. We track each debater’s six best SOTY-eligible speaker finishes.

Club of the Year (COTY) / Qual points
- COTY reuses the team-points table, but the credit is recorded on individual debaters. Every qualifying partnership that clears feeds its points into each debater’s personal total. A debater’s annual contribution to their school is capped at 60, and every autoqual (placing at or above the autoqual bar at select BP and exansion tournaments) adds a 6-point bonus on top of the capped value, leading to a 66 point maximum contribution. School totals are the sum of those capped contributions, so “qual points” and “COTY points” refer to the same concept, although "qual points" typically refers to the uncapped quantity (can go above 60) without the 6 point bonus.

Tournaments that do *not* hand out season points
- Nationals and British Parliamentary (BP) weekends award autoqual bids but no season points.
- Gender Minority, BIPOC, and similar invitationals award COTY (qual) credit only.

General notes
- Tournament size drives the base, so hosting a larger varsity field increases the reward for deep runs. Hosting schools usually abstain from competing at their own tournaments, but that rarely swings season-long races.
- Hybrid teams still grant SOTY/COTY credit to their members even though the partnership itself cannot bank TOTY markers.

For bylaw-level detail, see https://apda.online/2025/09/02/bylaws/
"""

    def get(self, request):
        return HttpResponse(self.body, content_type="text/plain; charset=utf-8")
