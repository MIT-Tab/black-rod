"""Administrator discovery and automatic tournament matching for results imports."""
from datetime import date, timedelta
from urllib.parse import urlencode

import requests
from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from core.import_sources import (
    fetch_import_source, fresh_or_cached_source, save_review, source_index, source_key,
)
from core.live_results import _directory_urls
from core.models import School, Tournament


def match_tournament(payload):
    metadata = payload["tournament"]
    held_on = date.fromisoformat(metadata["date"])
    host_id = (metadata.get("host_school") or {}).get("apda_id")
    if not School.objects.filter(pk=host_id).exists():
        raise ValueError("Set this tournament's APDA host school in mit-tab before importing.")
    matches = Tournament.objects.filter(
        host_id=host_id, season=str(metadata["season"]),
        qual_type=int(metadata["tournament_type"]),
        date__range=(held_on - timedelta(days=7), held_on + timedelta(days=7)),
    )
    if matches.count() > 1:
        raise ValueError("Multiple tournaments match this source. Correct their dates before importing.")
    tournament = matches.first()
    if tournament:
        return tournament
    return Tournament.objects.create(
        host_id=host_id, season=str(metadata["season"]), date=held_on,
        qual_type=int(metadata["tournament_type"]),
    )


class ImportSourcesView(UserPassesTestMixin, View):
    def test_func(self):
        return self.request.user.is_superuser

    def sources(self):
        rows = source_index()
        for url in _directory_urls():
            key = source_key(url)
            rows.setdefault(key, {"url": url, "name": url, "missing_since": None})
        return rows

    def get(self, request):
        return render(request, "tournaments/import_sources.html", {"sources": self.sources()})

    def post(self, request):
        key = request.POST.get("source")
        try:
            rows = self.sources()
            if key not in rows:
                raise ValueError("This tournament is no longer available.")
            payload = (fresh_or_cached_source(key) if key in source_index()
                       else fetch_import_source(rows[key]["url"]))
            if not payload.get("import_ready") or not payload.get("bundle"):
                raise ValueError("Results are still embargoed. Try after outrounds are posted or three hours after the final inround.")
            tournament = match_tournament(payload)
            token = save_review(payload, tournament.id, request.user.id)
        except (ValueError, requests.RequestException) as exc:
            messages.error(request, str(exc))
            return redirect("core:import_sources")
        params = urlencode({"tournament": tournament.id, "review": token,
                            "api_url": rows[key]["url"]})
        return redirect(f"{reverse('core:tournament_dataentry')}?{params}")
