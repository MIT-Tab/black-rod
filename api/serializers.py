from django.urls import reverse

from core.utils.rankings import place_as_round


def _absolute_url(request, relative_url):
    """
    Turn a relative path into an absolute URL when the request is available.
    Django's build_absolute_uri gracefully handles already-absolute URLs.
    """
    if not request:
        return relative_url
    return request.build_absolute_uri(relative_url)


def serialize_school(school, request=None):
    data = {
        "id": school.id,
        "name": school.name,
    }
    if hasattr(school, "included_in_oty"):
        data["included_in_oty"] = school.included_in_oty
    if request:
        data["url"] = _absolute_url(request, school.get_absolute_url())
        data["api_url"] = _absolute_url(
            request,
            reverse("api:school_detail", args=[school.id])
        )
    return data


def serialize_debater(debater, request=None):
    school_payload = serialize_school(debater.school, request) if debater.school else None
    data = {
        "id": debater.id,
        "name": debater.name,
        "first_name": debater.first_name,
        "last_name": debater.last_name,
        "status": debater.get_status_display(),
        "status_code": debater.status,
        "school": school_payload,
    }
    if request:
        data["url"] = _absolute_url(request, debater.get_absolute_url())
        data["api_url"] = _absolute_url(
            request,
            reverse("api:debater_detail", args=[debater.id])
        )
    return data


def serialize_team(team, request=None, include_debaters=True):
    debaters_payload = []
    if include_debaters:
        debaters_payload = [serialize_debater(debater, request) for debater in team.debaters.all()]
    data = {
        "id": team.id,
        "name": team.name,
        "hybrid": team.hybrid,
        "debaters": debaters_payload,
    }
    if request:
        data["url"] = _absolute_url(request, team.get_absolute_url())
        data["api_url"] = _absolute_url(
            request,
            reverse("api:team_detail", args=[team.id])
        )
    return data


def serialize_tournament(tournament, request=None):
    data = {
        "id": tournament.id,
        "name": tournament.name,
        "season": tournament.season,
        "season_display": tournament.get_season_display(),
        "date": tournament.date.isoformat() if tournament.date else None,
        "display": getattr(tournament, "display", tournament.name),
        "num_teams": tournament.num_teams,
        "num_novice_debaters": tournament.num_novice_debaters,
    }
    if tournament.host:
        data["host"] = serialize_school(tournament.host, request)
    if request:
        data["url"] = _absolute_url(request, tournament.get_absolute_url())
        data["api_url"] = _absolute_url(
            request,
            reverse("api:tournament_detail", args=[tournament.id])
        )
    return data


def serialize_video(video, request=None):
    data = {
        "id": video.id,
        "round": video.get_round_display(),
        "round_code": video.round,
        "link": video.link,
        "description": video.description,
        "case": video.case,
        "permissions": video.get_permissions_display(),
        "tournament": serialize_tournament(video.tournament, request),
        "participants": {
            "pm": serialize_debater(video.pm, request),
            "mg": serialize_debater(video.mg, request),
            "lo": serialize_debater(video.lo, request),
            "mo": serialize_debater(video.mo, request),
        },
        "tags": [tag.name for tag in video.tags.all()],
    }
    if request:
        data["url"] = _absolute_url(request, video.get_absolute_url())
    return data


def serialize_team_result(result, request=None):
    place_display = ""
    if result.place and result.place > 0:
        place_display = place_as_round(result.place)
    return {
        "id": result.id,
        "place": result.place,
        "place_display": place_display,
        "type": result.get_type_of_place_display(),
        "type_code": result.type_of_place,
        "ghost_points": result.ghost_points,
        "team": serialize_team(result.team, request),
        "tournament": serialize_tournament(result.tournament, request),
    }


def serialize_speaker_result(result, request=None):
    return {
        "id": result.id,
        "place": result.place,
        "type": result.get_type_of_place_display(),
        "type_code": result.type_of_place,
        "tie": result.tie,
        "debater": serialize_debater(result.debater, request),
        "tournament": serialize_tournament(result.tournament, request),
    }
