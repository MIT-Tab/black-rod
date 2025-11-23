from django.http import JsonResponse
from django.templatetags.static import static
from django.urls import reverse


def _absolute(request, path):
    return request.build_absolute_uri(path)


def ai_plugin_manifest(request):
    schema_url = _absolute(request, reverse("core:openapi_schema"))
    logo_url = _absolute(request, static("favicon.ico"))
    manifest = {
        "schema_version": "v1",
        "name_for_human": "APDA Standings & Profiles",
        "name_for_model": "apdaStandings",
        "description_for_human": (
            "Search APDA standings, school rosters, debater histories, "
            "team records, and tournament breakdowns in structured formats."
        ),
        "description_for_model": (
            "Use this plugin to fetch APDA standings and entity details. "
            "It exposes JSON endpoints for Team/Speaker of the Year boards "
            "and detail pages for schools, debaters, teams, and tournaments."
        ),
        "auth": {"type": "none"},
        "api": {
            "type": "openapi",
            "url": schema_url,
            "is_user_authenticated": False,
        },
        "logo_url": logo_url,
        "contact_email": "info@apda.online",
        "legal_info_url": _absolute(request, "/"),
    }
    return JsonResponse(manifest)


def openapi_schema(request):
    server_url = request.build_absolute_uri("/").rstrip("/")
    schema = {
        "openapi": "3.0.1",
        "info": {
            "title": "APDA Standings API",
            "version": "1.0.0",
            "description": "Machine-friendly endpoints for APDA standings and entity details.",
        },
        "servers": [{"url": server_url}],
        "paths": {
            "/api/standings/": {
                "get": {
                    "summary": "Season standings",
                    "description": "Fetch TOTY, COTY, SOTY, NOTY, and online qualifier standings for a season.",
                    "parameters": [
                        {
                            "name": "season",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": "APDA season (e.g., '2024'). Defaults to the current season.",
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Standings payload organized by board.",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            },
            "/api/debaters/{debater_id}/detail/": {
                "get": {
                    "summary": "Debater profile",
                    "parameters": [
                        {
                            "name": "debater_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                            "description": "Primary key of the debater.",
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Full profile including standings, partners, seasons, and videos.",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            },
            "/api/teams/{team_id}/detail/": {
                "get": {
                    "summary": "Team profile",
                    "parameters": [
                        {
                            "name": "team_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                            "description": "Primary key of the team.",
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Team roster, TOTY placements, and tournament performances.",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            },
            "/api/tournaments/{tournament_id}/detail/": {
                "get": {
                    "summary": "Tournament detail",
                    "parameters": [
                        {
                            "name": "tournament_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                            "description": "Primary key of the tournament.",
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Tournament metadata, team awards, speaker awards, and tab cards.",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            },
            "/api/schools/{school_id}/detail/": {
                "get": {
                    "summary": "School profile by season",
                    "parameters": [
                        {
                            "name": "school_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                            "description": "Primary key of the school.",
                        },
                        {
                            "name": "season",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": "Season to summarize (defaults to current).",
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Seasonal roster, COTY breakdown, and hosted tournament list.",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            },
        },
    }
    return JsonResponse(schema)
