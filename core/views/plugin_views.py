from django.http import JsonResponse
from django.templatetags.static import static
from django.urls import reverse
from drf_spectacular.views import OpenApiJsonRenderer, SpectacularAPIView


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


def _build_schema_servers(request):
    host = request.get_host().split(":")[0]
    primary = f"https://{host}"
    return [{"url": primary}]


class PluginSchemaView(SpectacularAPIView):
    """Return the OpenAPI schema in JSON with stable server metadata."""

    renderer_classes = [OpenApiJsonRenderer]
    schema = None

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        schema = response.data or {}
        schema["openapi"] = "3.1.0"
        schema["servers"] = _build_schema_servers(request)
        schema["security"] = []
        components = schema.setdefault("components", {})
        components["securitySchemes"] = {}
        response.data = schema
        return response
