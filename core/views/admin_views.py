import os
import re
from collections import defaultdict
from difflib import SequenceMatcher

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q, Min
from django.db.models.functions import Lower
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView

from core.models import DebaterAliasGroup, SOTY, TOTY, Debater, Team, MergeDebaterRequest
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


class MergeSuggestionsView(UserPassesTestMixin, TemplateView):
    template_name = "admin/merge_suggestions.html"
    max_suggestions = 200

    def test_func(self):
        return self.request.user.is_superuser

    def post(self, request, *args, **kwargs):
        """Handle creating a merge request via AJAX."""
        debater_one_id = request.POST.get("debater_one")
        debater_two_id = request.POST.get("debater_two")

        if not debater_one_id or not debater_two_id:
            return JsonResponse({"success": False, "error": "Missing debater selection."})

        try:
            debater_one = Debater.objects.select_related("school").get(pk=debater_one_id)
            debater_two = Debater.objects.select_related("school").get(pk=debater_two_id)
        except Debater.DoesNotExist:
            return JsonResponse({"success": False, "error": "Unable to find one of the selected debaters."})

        # Ensure the debater with more results is kept as primary (avoid N+1 queries)
        debaters_with_counts = Debater.objects.filter(
            pk__in=[debater_one.pk, debater_two.pk]
        ).annotate(
            team_result_count=Count('teams__team_results', distinct=True),
            speaker_result_count=Count('speaker_results', distinct=True),
            round_stat_count=Count('round_stats', distinct=True),
            pm_video_count=Count('pm_videos', distinct=True),
            lo_video_count=Count('lo_videos', distinct=True),
            mg_video_count=Count('mg_videos', distinct=True),
            mo_video_count=Count('mo_videos', distinct=True),
        ).select_related('school')
        
        debaters_dict = {d.pk: d for d in debaters_with_counts}
        debater_one_annotated = debaters_dict.get(debater_one.pk)
        debater_two_annotated = debaters_dict.get(debater_two.pk)
        
        # Calculate totals and determine primary/secondary
        if debater_one_annotated and debater_two_annotated:
            one_total = (
                debater_one_annotated.team_result_count +
                debater_one_annotated.speaker_result_count +
                debater_one_annotated.round_stat_count +
                debater_one_annotated.pm_video_count +
                debater_one_annotated.lo_video_count +
                debater_one_annotated.mg_video_count +
                debater_one_annotated.mo_video_count
            )
            two_total = (
                debater_two_annotated.team_result_count +
                debater_two_annotated.speaker_result_count +
                debater_two_annotated.round_stat_count +
                debater_two_annotated.pm_video_count +
                debater_two_annotated.lo_video_count +
                debater_two_annotated.mg_video_count +
                debater_two_annotated.mo_video_count
            )
            
            # Swap if debater_two has more results
            if two_total > one_total:
                primary_debater = debater_two
                secondary_debater = debater_one
            else:
                primary_debater = debater_one
                secondary_debater = debater_two
        else:
            # Fallback if annotations fail
            primary_debater = debater_one
            secondary_debater = debater_two

        # Check if request already exists
        existing = MergeDebaterRequest.objects.filter(
            Q(primary_debater=primary_debater, secondary_debater=secondary_debater) |
            Q(primary_debater=secondary_debater, secondary_debater=primary_debater)
        ).filter(status=MergeDebaterRequest.STATUS_PENDING).first()
        
        if existing:
            return JsonResponse({"success": False, "error": "A pending merge request already exists for these debaters."})

        # Create merge request
        MergeDebaterRequest.objects.create(
            requested_by=request.user,
            primary_debater=primary_debater,
            secondary_debater=secondary_debater,
            primary_name=primary_debater.name if primary_debater else "",
            primary_school_name=primary_debater.school.name if primary_debater and primary_debater.school else "",
            secondary_name=secondary_debater.name if secondary_debater else "",
            secondary_school_name=secondary_debater.school.name if secondary_debater and secondary_debater.school else "",
        )
        
        # Clear the cache so next page load shows updated suggestions
        cache.delete('merge_suggestions_list')
        
        return JsonResponse({
            "success": True,
            "message": f"Merge request created for {primary_debater.name} and {secondary_debater.name}."
        })

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Check if we should force refresh (via query param)
        force_refresh = self.request.GET.get('refresh') == '1'
        
        # Try to get from cache first (cache for 5 minutes)
        cache_key = 'merge_suggestions_list'
        if not force_refresh:
            cached_suggestions = cache.get(cache_key)
            if cached_suggestions is not None:
                context["suggestions"] = cached_suggestions
                context["cached"] = True
                return context
        
        suggestions = self._build_suggestions()
        
        # Cache the results for 5 minutes
        cache.set(cache_key, suggestions, 300)
        
        context["suggestions"] = suggestions
        context["cached"] = False
        return context

    def _build_suggestions(self):
        """Build a list of potential merge candidates with scoring."""
        # Strategy: Group by exact normalized first+last name, then do fuzzy matching
        # This is MUCH faster than comparing all pairs
        
        # Get IDs of debaters who already have pending merge requests
        pending_debater_ids = set()
        pending_requests = MergeDebaterRequest.objects.filter(
            status=MergeDebaterRequest.STATUS_PENDING
        ).values_list('primary_debater_id', 'secondary_debater_id')
        
        for primary_id, secondary_id in pending_requests:
            if primary_id:
                pending_debater_ids.add(primary_id)
            if secondary_id:
                pending_debater_ids.add(secondary_id)
        
        # First, get recent debaters WITHOUT the expensive counts
        # We'll calculate counts only for those we actually need
        debaters = list(
            Debater.objects.filter(alias_group__isnull=True)
            .exclude(first_name__isnull=True)
            .exclude(first_name="")
            .exclude(last_name__isnull=True)
            .exclude(last_name="")
            .exclude(id__in=pending_debater_ids)  # Exclude debaters with pending requests
            .select_related("school")
            .annotate(
                first_normalized=Lower("first_name"),
                last_normalized=Lower("last_name"),
            )
            .order_by("-id")[:2000]  # Only look at most recent 2000
        )
        
        # Group by exact normalized full name first to find exact matches
        name_groups = defaultdict(list)
        for debater in debaters:
            key = (debater.first_normalized, debater.last_normalized)
            name_groups[key].append(debater)
        
        # Also group by first letter for fuzzy matching
        first_letter_groups = defaultdict(list)
        for debater in debaters:
            if debater.first_normalized:
                first_letter_groups[debater.first_normalized[0]].append(debater)
        
        # Collect pairs to check
        pairs_to_check = []
        
        # First pass: exact name matches
        for (first, last), group in name_groups.items():
            if len(group) < 2:
                continue
            
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    pairs_to_check.append((group[i], group[j], 1.0))
        
        # Second pass: fuzzy matches within same first letter (limit to small groups)
        for letter, group in first_letter_groups.items():
            if len(group) > 100:  # Skip large groups
                continue
                
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    first_debater = group[i]
                    second_debater = group[j]
                    
                    # Skip if already in exact match
                    if (first_debater.first_normalized, first_debater.last_normalized) == \
                       (second_debater.first_normalized, second_debater.last_normalized):
                        continue
                    
                    # Calculate similarity
                    name_similarity = self._calculate_name_similarity(first_debater, second_debater)
                    
                    if name_similarity < 0.75:
                        continue
                    
                    pairs_to_check.append((first_debater, second_debater, name_similarity))
        
        # Now fetch counts only for debaters in our pairs
        debater_ids_to_count = set()
        for first, second, _ in pairs_to_check:
            debater_ids_to_count.add(first.id)
            debater_ids_to_count.add(second.id)
        
        # Fetch counts in one query for all relevant debaters
        if debater_ids_to_count:
            debaters_with_counts = {
                d.id: d for d in Debater.objects.filter(id__in=debater_ids_to_count).annotate(
                    team_result_count=Count('teams__team_results', distinct=True),
                    speaker_result_count=Count('speaker_results', distinct=True),
                    round_stat_count=Count('round_stats', distinct=True),
                    video_count=Count('pm_videos', distinct=True) + 
                               Count('lo_videos', distinct=True) + 
                               Count('mg_videos', distinct=True) + 
                               Count('mo_videos', distinct=True),
                )
            }
            
            # Add total_results to each
            for debater in debaters_with_counts.values():
                debater.total_results = (
                    debater.team_result_count +
                    debater.speaker_result_count +
                    debater.round_stat_count +
                    debater.video_count
                )
        else:
            debaters_with_counts = {}
        
        # Create suggestions from pairs
        suggestions = []
        for first, second, name_similarity in pairs_to_check:
            # Get the versions with counts
            first_with_counts = debaters_with_counts.get(first.id, first)
            second_with_counts = debaters_with_counts.get(second.id, second)
            
            # Add total_results if not already set
            if not hasattr(first_with_counts, 'total_results'):
                first_with_counts.total_results = 0
            if not hasattr(second_with_counts, 'total_results'):
                second_with_counts.total_results = 0
            
            suggestion = self._create_suggestion(first_with_counts, second_with_counts, name_similarity)
            if suggestion:
                suggestions.append(suggestion)
        
        # Sort by score (highest first)
        suggestions.sort(key=lambda x: x["score"], reverse=True)
        
        return suggestions[:self.max_suggestions]

    def _create_suggestion(self, first, second, name_similarity):
        """Create a suggestion dict if the pair qualifies."""
        score = self._calculate_merge_score(first, second, name_similarity)
        
        if score <= 0:
            return None
        
        min_results = min(first.total_results, second.total_results)
        max_results = max(first.total_results, second.total_results)
        
        return {
            "debater_one": first,
            "debater_two": second,
            "score": score,
            "name_similarity": name_similarity,
            "same_school": first.school_id == second.school_id,
            "min_results": min_results,
            "max_results": max_results,
        }

    def _calculate_name_similarity(self, first, second):
        """Calculate fuzzy match score between two debater names (0-1)."""
        name1 = f"{first.first_name} {first.last_name}".lower().strip()
        name2 = f"{second.first_name} {second.last_name}".lower().strip()
        
        return SequenceMatcher(None, name1, name2).ratio()

    def _calculate_merge_score(self, first, second, name_similarity):
        """
        Calculate a score for how likely two debaters should be merged.
        Higher score = more likely to need merging.
        """
        score = 0
        
        # Factor 1: Name similarity (0-100 points)
        score += name_similarity * 100
        
        # Factor 2: Recency (0-50 points)
        max_id = max(first.id, second.id)
        recency_score = min(max_id / 200, 50)
        score += recency_score
        
        # Factor 3: Same school (0-30 points)
        if first.school_id == second.school_id:
            score += 30
        
        # Factor 4: Imbalanced results (0-40 points)
        min_results = min(first.total_results, second.total_results)
        max_results = max(first.total_results, second.total_results)
        
        if max_results > 0:
            if min_results <= 5:
                imbalance_ratio = 1.0 - (min_results / (max_results + 1))
                score += imbalance_ratio * 40
            else:
                # Both have many results, less likely to be duplicate
                score -= 20
        
        return score
