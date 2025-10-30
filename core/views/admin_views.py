import os
import re
from collections import defaultdict

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.db import transaction
from django.db.models.functions import Lower
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView

from core.models import DebaterAliasGroup, SOTY, TOTY, Debater, Team
from core.utils.rankings import redo_rankings, update_noty, update_soty, update_toty


class AdminToolsView(UserPassesTestMixin, TemplateView):
    template_name = "admin/admin_tools.html"

    def test_func(self):
        return self.request.user.is_superuser


class DebaterAliasSuggestionView(UserPassesTestMixin, TemplateView):
    template_name = "admin/debater_alias_suggestions.html"
    max_suggestions = 200

    def test_func(self):
        return self.request.user.is_superuser

    def post(self, request, *args, **kwargs):
        debater_one_id = request.POST.get("debater_one")
        debater_two_id = request.POST.get("debater_two")

        if not debater_one_id or not debater_two_id:
            messages.error(request, "Missing debater selection.")
            return redirect("core:debater_alias_suggestions")

        try:
            debater_one = Debater.objects.select_related("alias_group").get(
                pk=debater_one_id
            )
            debater_two = Debater.objects.select_related("alias_group").get(
                pk=debater_two_id
            )
        except Debater.DoesNotExist:
            messages.error(request, "Unable to find one of the selected debaters.")
            return redirect("core:debater_alias_suggestions")

        self._link_debaters(debater_one, debater_two)
        messages.success(
            request,
            f"Linked {debater_one.name} ({debater_one.school}) and {debater_two.name} ({debater_two.school}).",
        )
        return redirect("core:debater_alias_suggestions")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["suggestions"] = self._build_suggestions()
        return context

    def _build_suggestions(self):
        debaters = (
            Debater.objects.annotate(
                first_normalized=Lower("first_name"),
                last_normalized=Lower("last_name"),
            )
            .exclude(first_name__isnull=True)
            .exclude(first_name="")
            .exclude(last_name__isnull=True)
            .exclude(last_name="")
            .select_related("school", "alias_group")
            .order_by("-id")
        )

        grouped = defaultdict(list)
        for debater in debaters:
            key = (debater.first_normalized, debater.last_normalized)
            grouped[key].append(debater)

        suggestions = []
        for matches in grouped.values():
            if len(matches) < 2:
                continue
            matches.sort(key=lambda deb: deb.id, reverse=True)
            for i in range(len(matches)):
                for j in range(i + 1, len(matches)):
                    first = matches[i]
                    second = matches[j]
                    if first.school_id == second.school_id:
                        continue
                    if first.alias_group_id and first.alias_group_id == second.alias_group_id:
                        continue
                    season_gap = self._season_gap(first, second)
                    if season_gap is not None and season_gap > 3:
                        continue
                    suggestions.append(
                        {
                            "debater_one": first,
                            "debater_two": second,
                            "order_key": max(first.id, second.id),
                            "season_gap": season_gap,
                        }
                    )

        suggestions.sort(
            key=lambda item: (
                item["season_gap"] is None,
                item["season_gap"] if item["season_gap"] is not None else float("inf"),
                -item["order_key"],
            )
        )
        return suggestions[: self.max_suggestions]

    def _season_gap(self, first, second):
        comparisons = [
            (first.first_season, second.first_season),
            (first.latest_season, second.latest_season),
            (first.first_season, second.latest_season),
            (first.latest_season, second.first_season),
        ]
        gaps = []
        for a, b in comparisons:
            year_a = self._parse_season(a)
            year_b = self._parse_season(b)
            if year_a is not None and year_b is not None:
                gaps.append(abs(year_a - year_b))
        if gaps:
            return min(gaps)
        return None

    @staticmethod
    def _parse_season(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _link_debaters(debater_one, debater_two):
        with transaction.atomic():
            primary_group = debater_one.alias_group or debater_two.alias_group

            if debater_one.alias_group and debater_two.alias_group:
                if debater_one.alias_group_id != debater_two.alias_group_id:
                    primary_group = debater_one.alias_group
                    old_group = debater_two.alias_group
                    Debater.objects.filter(alias_group=old_group).update(
                        alias_group=primary_group
                    )
                    old_group.delete()

            if not primary_group:
                primary_group = DebaterAliasGroup.objects.create(
                    label=debater_one.name.strip() or debater_two.name.strip()
                )

            if debater_one.alias_group_id != primary_group.id:
                Debater.objects.filter(pk=debater_one.pk).update(
                    alias_group=primary_group
                )
            if debater_two.alias_group_id != primary_group.id:
                Debater.objects.filter(pk=debater_two.pk).update(
                    alias_group=primary_group
                )


class MitTabDashboardView(UserPassesTestMixin, TemplateView):
    template_name = "admin/mittab_dashboard.html"

    def test_func(self):
        return self.request.user.is_superuser

    def get_tournament_data(self):
        nu_tab_url = os.environ.get("NU_TAB_URL", "https://nu-tab.com")
        tournaments = []
        error_message = None

        try:
            response = requests.get(nu_tab_url, timeout=10)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")
            content_div = soup.find("div", {"id": "content"})

            if not content_div:
                error_message = "Content div not found in the response"
                return tournaments, error_message

            links = content_div.find_all("a", href=True)

            for link in links:
                href = link.get("href", "")
                text = link.get_text(strip=True)

                match = re.match(r"^(.*?)\.nu-tab\.com$", text)
                if match:
                    tournament_name = match.group(1)
                    if href.startswith("http"):
                        tournament_url = href
                    else:
                        tournament_url = f"http://{text}"

                    tournaments.append({"name": tournament_name, "url": tournament_url})

        except requests.RequestException as e:
            error_message = f"Failed to fetch data from nu-tab.com: {str(e)}"
        except Exception as e:
            error_message = f"Error parsing tournament data: {str(e)}"

        return tournaments, error_message

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tournaments, error_message = self.get_tournament_data()

        context["tournaments"] = tournaments
        context["error_message"] = error_message
        context["nu_tab_url"] = os.environ.get("NU_TAB_URL", "https://nu-tab.com")

        return context


class RankingsRecomputeView(UserPassesTestMixin, TemplateView):
    template_name = "admin/rankings_recompute.html"

    def test_func(self):
        return self.request.user.is_superuser

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["seasons"] = settings.SEASONS
        context["ranking_types"] = [
            ("toty", "TOTY"),
            ("soty", "SOTY"),
            ("noty", "NOTY"),
        ]
        return context

    def post(self, request, *args, **kwargs):
        season = request.POST.get("season")
        ranking_type = request.POST.get("ranking_type")

        if not season or not ranking_type:
            return JsonResponse(
                {"success": False, "error": "Season and ranking type are required"}
            )

        try:
            ranking_funcs = {
                "toty": lambda: self._update_toty_rankings(season),
                "soty": lambda: self._update_soty_rankings(season),
                "noty": lambda: self._update_noty_rankings(season),
            }

            if ranking_type in ranking_funcs:
                ranking_funcs[ranking_type]()
                return JsonResponse(
                    {
                        "success": True,
                        "message": f"Successfully recomputed {ranking_type.upper()} rankings for season {season}",
                    }
                )

        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)})

    def _update_toty_rankings(self, season):
        for team in Team.objects.all():
            update_toty(team, season=season)
        redo_rankings(
            TOTY.objects.filter(season=season), season=season, cache_type="toty"
        )

    def _update_soty_rankings(self, season):
        for debater in Debater.objects.all():
            update_soty(debater, season=season)
        redo_rankings(
            SOTY.objects.filter(season=season), season=season, cache_type="soty"
        )

    def _update_noty_rankings(self, season):
        for debater in Debater.objects.all():
            update_noty(debater, season=season)
