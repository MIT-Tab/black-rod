import os
import re
from hashlib import md5
from collections import defaultdict
from difflib import SequenceMatcher
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, F, Q, Min
from django.db.models.functions import Lower
from django.forms import modelformset_factory
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView

from core.forms import RoundAmendmentUploadForm
from core.models import (
    DebaterAliasGroup,
    MergeDebaterRequest,
    Debater,
    RoundStats,
    School,
    SOTY,
    SpeakerResult,
    Team,
    TeamResult,
    TOTY,
    Video,
)
from core.utils.rankings import redo_rankings, update_noty, update_soty, update_toty
from core.utils.elo_runtime_engine.cache import clear_runtime_caches
from core.utils.round_amendments import (
    RoundAmendmentError,
    apply_round_amendments,
    load_round_amendment_document,
)
from core.utils.round_amendment_recorder import (
    build_synthetic_resolution_action,
    record_round_amendment_action,
    round_amendment_recording_context,
)
from core.utils.synthetic_cleanup import (
    get_synthetic_entity,
    parse_selection_token,
    synthetic_cleanup_sections,
    synthetic_entity_is_unreferenced,
    synthetic_entity_reference_summary,
)
from core.utils.synthetic_resolution import resolve_synthetic_entity
from core.views.elo_cache import invalidate_cached_elo_state


class AdminToolsView(UserPassesTestMixin, TemplateView):
    template_name = "admin/admin_tools.html"

    def test_func(self):
        return self.request.user.is_superuser

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["round_amendment_form"] = kwargs.get("round_amendment_form") or RoundAmendmentUploadForm()
        context["round_amendment_recording"] = round_amendment_recording_context()
        return context


class SyntheticCleanupView(UserPassesTestMixin, TemplateView):
    template_name = "admin/synthetic_cleanup.html"

    def test_func(self):
        return self.request.user.is_superuser

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sections = synthetic_cleanup_sections()
        context["sections"] = sections
        context["total_count"] = sum(section["count"] for section in sections)
        return context

    def post(self, request, *args, **kwargs):
        selections = request.POST.getlist("selected_ids")
        if not selections:
            messages.info(request, "Select at least one synthetic entity to delete.")
            return redirect("core:synthetic_cleanup")

        deleted = []
        skipped = []

        for token in selections:
            entity_type, object_id = parse_selection_token(token)
            obj = get_synthetic_entity(entity_type, object_id)
            if obj is None:
                skipped.append(f"{token} (missing)")
                continue

            if not synthetic_entity_is_unreferenced(obj):
                reference_labels = ", ".join(
                    f"{item['label']} ({item['count']})"
                    for item in synthetic_entity_reference_summary(obj)
                )
                skipped.append(f"{entity_type}:{obj.pk} ({reference_labels})")
                continue

            obj.delete()
            deleted.append(f"{entity_type}:{object_id}")

        if deleted:
            messages.success(request, f"Deleted {len(deleted)} unreferenced synthetic entit{'y' if len(deleted) == 1 else 'ies'}.")
        if skipped:
            messages.warning(
                request,
                "Skipped %d selection%s because they were missing or are now referenced: %s"
                % (
                    len(skipped),
                    "" if len(skipped) == 1 else "s",
                    "; ".join(skipped[:5]),
                ),
            )

        return redirect("core:synthetic_cleanup")


class SyntheticResolutionView(UserPassesTestMixin, TemplateView):
    template_name = "admin/admin_tools.html"

    def test_func(self):
        return self.request.user.is_superuser

    def post(self, request, *args, **kwargs):
        entity_type = str(request.POST.get("entity_type") or "").strip().lower()
        synthetic_id = request.POST.get("synthetic_id")
        target_id = request.POST.get("target_id")
        reason = str(request.POST.get("reason") or "").strip()

        try:
            resolve_synthetic_entity(
                entity_type=entity_type,
                synthetic_id=int(synthetic_id),
                target_id=int(target_id),
                actor=request.user,
                reason=reason,
                source_context={"source": "admin_tools"},
            )
            messages.success(request, "Synthetic entity resolved successfully.")
            try:
                recorded_path = record_round_amendment_action(
                    build_synthetic_resolution_action(
                        entity_type=entity_type,
                        synthetic_id=int(synthetic_id),
                        target_id=int(target_id),
                        reason=reason,
                    )
                )
            except Exception as exc:
                messages.warning(request, "Synthetic entity resolved but amendment recording failed: %s" % exc)
            else:
                if recorded_path:
                    messages.info(
                        request,
                        f"Recorded amendment action to {recorded_path}.",
                    )
        except Exception as exc:
            messages.error(request, "Synthetic resolution failed: %s" % exc)

        return redirect("core:admin_tools")


class EloCacheInvalidateView(UserPassesTestMixin, TemplateView):
    template_name = "admin/admin_tools.html"

    def test_func(self):
        return self.request.user.is_superuser

    def post(self, request, *args, **kwargs):
        version = invalidate_cached_elo_state()
        clear_runtime_caches()
        messages.success(
            request,
            f"ELO cache invalidated successfully. Active namespace version: {version}.",
        )
        return redirect("core:admin_tools")


class RoundAmendmentUploadView(AdminToolsView):
    def post(self, request, *args, **kwargs):
        form = RoundAmendmentUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            messages.error(request, "Please upload a valid amendment JSON file.")
            return self.render_to_response(self.get_context_data(round_amendment_form=form))

        uploaded_file = form.cleaned_data["amendment_file"]
        try:
            document = load_round_amendment_document(uploaded_file)
            summary = apply_round_amendments(
                document,
                actor=request.user,
                source_context={
                    "source": "admin_tools_round_amendment_upload",
                    "file_name": str(getattr(uploaded_file, "name", "") or ""),
                },
            )
        except RoundAmendmentError as exc:
            messages.error(request, f"Round amendment upload failed: {exc}")
            return self.render_to_response(self.get_context_data(round_amendment_form=form))
        except Exception as exc:
            messages.error(request, f"Round amendment upload failed: {exc}")
            return self.render_to_response(self.get_context_data(round_amendment_form=form))

        messages.success(request, self._build_success_message(summary))
        return redirect("core:admin_tools")

    @staticmethod
    def _build_success_message(summary):
        detail_map = (
            ("synthetic_resolutions", "synthetic resolutions"),
            ("rounds_created", "rounds created"),
            ("rounds_updated", "rounds updated"),
            ("rounds_deleted", "rounds deleted"),
            ("rounds_moved", "rounds moved"),
            ("tournament_imports_deleted", "tournament imports deleted"),
            ("tournament_imports_moved", "tournament import move actions"),
            ("linked_source_imports_moved", "linked source imports moved"),
        )
        details = [
            f"{int(summary[key])} {label}"
            for key, label in detail_map
            if int(summary.get(key) or 0)
        ]
        detail_suffix = ""
        if details:
            detail_suffix = " " + "; ".join(details) + "."
        return f"Applied {int(summary.get('actions_applied') or 0)} amendment actions.{detail_suffix}"


SchoolShortNameFormSet = modelformset_factory(
    School,
    fields=("short_name",),
    extra=0,
    widgets={
        "short_name": forms.TextInput(
            attrs={
                "class": "form-control form-control-sm",
                "placeholder": "Short name",
            }
        )
    },
)


class SchoolShortNameAuditView(UserPassesTestMixin, TemplateView):
    template_name = "admin/school_short_name_audit.html"
    form_prefix = "schools"

    def test_func(self):
        return self.request.user.is_superuser

    def get_queryset(self):
        missing_short_name = (
            Q(short_name__isnull=True)
            | Q(short_name__exact="")
            | Q(short_name__iexact=F("name"))
        )

        return (
            School.objects.filter(missing_short_name)
            .annotate(
                toty_count=Count("debaters__teams__toty", distinct=True),
                soty_count=Count("debaters__soty", distinct=True),
                noty_count=Count("debaters__noty", distinct=True),
                online_qual_count=Count("debaters__online_qual", distinct=True),
                coty_count=Count("coty", distinct=True),
            )
            .annotate(
                appearance_total=(
                    F("toty_count")
                    + F("soty_count")
                    + F("noty_count")
                    + F("online_qual_count")
                    + F("coty_count")
                )
            )
            .order_by("-appearance_total", "name")
        )

    def get_formset(self, data=None):
        return SchoolShortNameFormSet(
            data=data,
            queryset=self.get_queryset(),
            prefix=self.form_prefix,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        formset = kwargs.get("formset") or self.get_formset()
        context.update(
            {
                "formset": formset,
                "has_results": formset.total_form_count() > 0,
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        formset = self.get_formset(data=request.POST)
        if formset.is_valid():
            changed_forms = [form for form in formset.forms if form.has_changed()]
            formset.save()
            if changed_forms:
                messages.success(
                    request, f"Saved {len(changed_forms)} short name(s)."
                )
            else:
                messages.info(request, "No updates were needed.")
            return redirect(request.path)
        messages.error(request, "Please fix the errors below.")
        return self.render_to_response(self.get_context_data(formset=formset))


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
                return tournaments, "Content div not found in the response"

            for link in content_div.find_all("a", href=True):
                href = link.get("href", "")
                text = link.get_text(strip=True)
                match = re.match(r"^(.*?)\.nu-tab\.com$", text)
                if not match:
                    continue
                tournament_name = match.group(1)
                tournament_url = href if href.startswith("http") else f"http://{text}"
                tournaments.append({"name": tournament_name, "url": tournament_url})

        except requests.RequestException as exc:
            error_message = f"Failed to fetch data from nu-tab.com: {exc}"
        except Exception as exc:  # pragma: no cover - defensive template surface
            error_message = f"Error parsing tournament data: {exc}"

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
                        "message": (
                            f"Successfully recomputed {ranking_type.upper()} "
                            f"rankings for season {season}"
                        ),
                    }
                )
        except Exception as exc:  # pragma: no cover - AJAX error surface
            return JsonResponse({"success": False, "error": str(exc)})

        return JsonResponse({"success": False, "error": "Unknown ranking type"})

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
    cache_key = "merge_suggestions_list"

    def test_func(self):
        return self.request.user.is_superuser

    @staticmethod
    def _annotate_debaters_with_counts(queryset):
        return queryset.annotate(
            team_result_count=Count("teams__team_results", distinct=True),
            speaker_result_count=Count("speaker_results", distinct=True),
            round_stat_count=Count("round_stats", distinct=True),
            pm_video_count=Count("pm_videos", distinct=True),
            lo_video_count=Count("lo_videos", distinct=True),
            mg_video_count=Count("mg_videos", distinct=True),
            mo_video_count=Count("mo_videos", distinct=True),
        ).select_related("school")

    @staticmethod
    def _set_total_results(debaters):
        for debater in debaters:
            debater.total_results = (
                getattr(debater, "team_result_count", 0)
                + getattr(debater, "speaker_result_count", 0)
                + getattr(debater, "round_stat_count", 0)
                + getattr(debater, "pm_video_count", 0)
                + getattr(debater, "lo_video_count", 0)
                + getattr(debater, "mg_video_count", 0)
                + getattr(debater, "mo_video_count", 0)
            )

    def post(self, request, *args, **kwargs):
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
        debaters_with_counts = self._annotate_debaters_with_counts(
            Debater.objects.filter(pk__in=[debater_one.pk, debater_two.pk])
        )

        debaters_dict = {d.pk: d for d in debaters_with_counts}
        debater_one_annotated = debaters_dict.get(debater_one.pk)
        debater_two_annotated = debaters_dict.get(debater_two.pk)

        # Calculate totals and determine primary/secondary
        if debater_one_annotated and debater_two_annotated:
            self._set_total_results([debater_one_annotated, debater_two_annotated])
            one_total = debater_one_annotated.total_results
            two_total = debater_two_annotated.total_results
            
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
        cache.delete(self.cache_key)

        return JsonResponse({
            "success": True,
            "message": f"Merge request created for {primary_debater.name} and {secondary_debater.name}."
        })

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Check if we should force refresh (via query param)
        force_refresh = self.request.GET.get('refresh') == '1'
        
        # Try to get from cache first (cache for 5 minutes)
        if not force_refresh:
            cached_suggestions = cache.get(self.cache_key)
            if cached_suggestions is not None:
                context["suggestions"] = cached_suggestions
                context["cached"] = True
                return context

        # Cache the results for 5 minutes
        suggestions = self._build_suggestions()
        cache.set(self.cache_key, suggestions, 300)
        
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
            Debater.objects.filter(alias_group__isnull=True, synthetic=False)
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
            if len(group) > 100:
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
                d.id: d
                for d in self._annotate_debaters_with_counts(
                    Debater.objects.filter(id__in=debater_ids_to_count)
                )
            }

            self._set_total_results(debaters_with_counts.values())
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


class SyntheticResolutionSuggestionsView(MergeSuggestionsView):
    template_name = "admin/synthetic_resolution_suggestions.html"
    cache_key = "synthetic_resolution_suggestions_list"
    cache_version_key = "synthetic_resolution_suggestions_version"
    max_debater_suggestions = 100
    max_school_suggestions = 60
    default_synthetic_debater_limit = 250
    default_synthetic_school_limit = 150
    default_canonical_debater_limit = 2500
    school_stop_words = {
        "college",
        "campus",
        "institute",
        "of",
        "school",
        "the",
        "university",
    }

    def get_context_data(self, **kwargs):
        context = TemplateView.get_context_data(self, **kwargs)
        selected_debater_ids = self._parse_selected_ids("synthetic_debaters")
        selected_school_ids = self._parse_selected_ids("synthetic_schools")
        ran = self.request.GET.get("run") == "1"

        selected_debaters = self._load_selected_debaters(selected_debater_ids)
        selected_schools = self._load_selected_schools(selected_school_ids)

        context.update(
            {
                "ran": ran,
                "cached": False,
                "debater_suggestions": [],
                "school_suggestions": [],
                "suggestion_total": 0,
                "selected_debaters": selected_debaters,
                "selected_schools": selected_schools,
                "refresh_query": self._build_refresh_query(
                    selected_debater_ids=selected_debater_ids,
                    selected_school_ids=selected_school_ids,
                ),
            }
        )

        if not ran:
            return context

        force_refresh = self.request.GET.get("refresh") == "1"
        cache_key = self._get_suggestions_cache_key(
            selected_debater_ids=selected_debater_ids,
            selected_school_ids=selected_school_ids,
        )

        if not force_refresh:
            cached_payload = cache.get(cache_key)
            if cached_payload is not None:
                context.update(cached_payload)
                context["cached"] = True
                context["suggestion_total"] = len(context["debater_suggestions"]) + len(
                    context["school_suggestions"]
                )
                return context

        payload = {
            "debater_suggestions": self._build_debater_suggestions(
                selected_debater_ids=selected_debater_ids,
                selected_school_ids=selected_school_ids,
            ),
            "school_suggestions": self._build_school_suggestions(
                selected_school_ids=selected_school_ids,
            ),
        }
        payload["suggestion_total"] = len(payload["debater_suggestions"]) + len(
            payload["school_suggestions"]
        )
        cache.set(cache_key, payload, 300)
        context.update(payload)
        return context

    def post(self, request, *args, **kwargs):
        entity_type = str(request.POST.get("entity_type") or "debater").strip().lower()
        synthetic_id = request.POST.get("synthetic_id") or request.POST.get("synthetic_debater")
        target_id = request.POST.get("target_id") or request.POST.get("canonical_debater")
        reason = str(request.POST.get("reason") or "").strip()

        if entity_type == "debater":
            reason = reason or "Suggested synthetic debater resolution"
        elif entity_type == "school":
            reason = reason or "Suggested synthetic school resolution"
        else:
            return JsonResponse({"success": False, "error": "Unsupported synthetic entity type."})

        if not synthetic_id or not target_id:
            return JsonResponse({"success": False, "error": "Missing resolution selection."})

        try:
            synthetic_id = int(synthetic_id)
            target_id = int(target_id)
        except (TypeError, ValueError):
            return JsonResponse({"success": False, "error": "Invalid resolution selection."})

        if synthetic_id == target_id:
            return JsonResponse(
                {"success": False, "error": "Synthetic and canonical records must be different."}
            )

        try:
            resolution_message = self._build_resolution_message(
                entity_type=entity_type,
                synthetic_id=synthetic_id,
                target_id=target_id,
            )
        except (Debater.DoesNotExist, School.DoesNotExist):
            return JsonResponse(
                {"success": False, "error": "Unable to find one of the selected records."}
            )

        try:
            resolve_synthetic_entity(
                entity_type=entity_type,
                synthetic_id=synthetic_id,
                target_id=target_id,
                actor=request.user,
                reason=reason,
                source_context={"source": "synthetic_resolution_suggestions"},
            )
        except Exception as exc:
            return JsonResponse({"success": False, "error": str(exc)})

        recording_warning = None
        try:
            record_round_amendment_action(
                build_synthetic_resolution_action(
                    entity_type=entity_type,
                    synthetic_id=synthetic_id,
                    target_id=target_id,
                    reason=reason,
                )
            )
        except Exception as exc:
            recording_warning = str(exc)

        self._bump_cache_version()

        payload = {
            "success": True,
            "message": resolution_message,
        }
        if recording_warning:
            payload["warning"] = (
                "Synthetic entity resolved, but amendment recording failed: "
                f"{recording_warning}"
            )
        return JsonResponse(payload)

    def _parse_selected_ids(self, param_name):
        ids = []
        seen_ids = set()
        for raw_value in self.request.GET.getlist(param_name):
            try:
                parsed = int(raw_value)
            except (TypeError, ValueError):
                continue
            if parsed in seen_ids:
                continue
            seen_ids.add(parsed)
            ids.append(parsed)
        return ids

    @staticmethod
    def _ordered_records(records, requested_ids):
        records_by_id = {record.id: record for record in records}
        return [records_by_id[record_id] for record_id in requested_ids if record_id in records_by_id]

    def _load_selected_debaters(self, selected_debater_ids):
        if not selected_debater_ids:
            return []
        records = Debater.all_objects.filter(
            pk__in=selected_debater_ids,
            synthetic=True,
        ).select_related("school")
        return self._ordered_records(records, selected_debater_ids)

    def _load_selected_schools(self, selected_school_ids):
        if not selected_school_ids:
            return []
        records = School.all_objects.filter(
            pk__in=selected_school_ids,
            synthetic=True,
        )
        return self._ordered_records(records, selected_school_ids)

    def _build_refresh_query(self, selected_debater_ids, selected_school_ids):
        params = [("run", "1"), ("refresh", "1")]
        params.extend(("synthetic_debaters", debater_id) for debater_id in selected_debater_ids)
        params.extend(("synthetic_schools", school_id) for school_id in selected_school_ids)
        return urlencode(params, doseq=True)

    def _get_suggestions_cache_key(self, selected_debater_ids, selected_school_ids):
        version = int(cache.get(self.cache_version_key) or 1)
        key_material = "|".join(
            [
                str(version),
                ",".join(str(value) for value in selected_debater_ids),
                ",".join(str(value) for value in selected_school_ids),
            ]
        )
        return f"{self.cache_key}:{md5(key_material.encode('utf-8')).hexdigest()}"

    def _bump_cache_version(self):
        current_version = int(cache.get(self.cache_version_key) or 1)
        cache.set(self.cache_version_key, current_version + 1, None)

    def _build_resolution_message(self, entity_type, synthetic_id, target_id):
        if entity_type == "debater":
            synthetic = Debater.all_objects.get(pk=synthetic_id, synthetic=True)
            canonical = Debater.objects.get(pk=target_id)
            return f"Resolved synthetic debater {synthetic.name} into {canonical.name}."
        if entity_type == "school":
            synthetic = School.all_objects.get(pk=synthetic_id, synthetic=True)
            canonical = School.objects.get(pk=target_id, synthetic=False)
            return f"Resolved synthetic school {synthetic.name} into {canonical.name}."
        raise ValueError("Unsupported synthetic entity type.")

    @staticmethod
    def _normalize_text(value):
        cleaned = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
        return " ".join(cleaned.split())

    def _normalize_school_text(self, value):
        normalized = self._normalize_text(value)
        tokens = [token for token in normalized.split() if token not in self.school_stop_words]
        compact = " ".join(tokens) or normalized
        return normalized, compact

    def _prepare_debater_name(self, debater):
        first = self._normalize_text(debater.first_name)
        last = self._normalize_text(debater.last_name)
        full = " ".join(part for part in [first, last] if part)
        return first, last, full

    def _candidate_debater_queryset(self, synthetic_debaters, selected_debater_ids):
        queryset = (
            Debater.objects.filter(alias_group__isnull=True, synthetic=False)
            .exclude(first_name__isnull=True)
            .exclude(first_name="")
            .exclude(last_name__isnull=True)
            .exclude(last_name="")
            .select_related("school")
            .order_by("-id")
        )

        if selected_debater_ids and len(synthetic_debaters) <= 20:
            last_prefix_filters = Q()
            school_ids = set()
            for synthetic_debater in synthetic_debaters:
                _, last_norm, _ = self._prepare_debater_name(synthetic_debater)
                if last_norm:
                    last_prefix_filters |= Q(last_name__istartswith=last_norm[:4])
                if synthetic_debater.school_id:
                    school_ids.add(synthetic_debater.school_id)
            if school_ids:
                last_prefix_filters |= Q(school_id__in=school_ids)
            if last_prefix_filters:
                queryset = queryset.filter(last_prefix_filters)
        else:
            queryset = queryset[: self.default_canonical_debater_limit]

        return list(queryset)

    def _build_debater_suggestions(self, selected_debater_ids, selected_school_ids):
        synthetic_filters = Q(alias_group__isnull=True, synthetic=True)
        synthetic_filters &= ~Q(first_name__isnull=True)
        synthetic_filters &= ~Q(first_name="")
        synthetic_filters &= ~Q(last_name__isnull=True)
        synthetic_filters &= ~Q(last_name="")

        if selected_debater_ids or selected_school_ids:
            scoped_filters = Q()
            if selected_debater_ids:
                scoped_filters |= Q(pk__in=selected_debater_ids)
            if selected_school_ids:
                scoped_filters |= Q(school_id__in=selected_school_ids)
            synthetic_filters &= scoped_filters

        synthetic_queryset = (
            Debater.all_objects.filter(synthetic_filters)
            .select_related("school")
            .order_by("-id")
        )
        if not selected_debater_ids and not selected_school_ids:
            synthetic_queryset = synthetic_queryset[: self.default_synthetic_debater_limit]
        synthetic_debaters = list(synthetic_queryset)
        if not synthetic_debaters:
            return []

        canonical_debaters = self._candidate_debater_queryset(
            synthetic_debaters=synthetic_debaters,
            selected_debater_ids=selected_debater_ids,
        )
        if not canonical_debaters:
            return []

        canonical_exact_name_groups = defaultdict(list)
        canonical_initial_last_groups = defaultdict(list)
        canonical_prefix_groups = defaultdict(list)
        canonical_school_initial_groups = defaultdict(list)
        canonical_by_id = {}
        for debater in canonical_debaters:
            first_norm, last_norm, _ = self._prepare_debater_name(debater)
            debater.first_normalized = first_norm
            debater.last_normalized = last_norm
            canonical_by_id[debater.id] = debater
            canonical_exact_name_groups[(first_norm, last_norm)].append(debater.id)
            canonical_initial_last_groups[(first_norm[:1], last_norm[:4])].append(debater.id)
            canonical_prefix_groups[(first_norm[:3], last_norm[:4])].append(debater.id)
            canonical_school_initial_groups[(debater.school_id, first_norm[:1])].append(debater.id)

        candidate_pairs = {}
        for synthetic_debater in synthetic_debaters:
            first_norm, last_norm, full_norm = self._prepare_debater_name(synthetic_debater)
            synthetic_debater.first_normalized = first_norm
            synthetic_debater.last_normalized = last_norm

            candidate_ids = []
            candidate_ids.extend(canonical_exact_name_groups.get((first_norm, last_norm), []))
            candidate_ids.extend(canonical_initial_last_groups.get((first_norm[:1], last_norm[:4]), []))
            candidate_ids.extend(canonical_prefix_groups.get((first_norm[:3], last_norm[:4]), []))
            if synthetic_debater.school_id:
                candidate_ids.extend(
                    canonical_school_initial_groups.get((synthetic_debater.school_id, first_norm[:1]), [])
                )

            seen_candidate_ids = set()
            ranked_candidates = []
            for candidate_id in candidate_ids:
                if candidate_id in seen_candidate_ids:
                    continue
                seen_candidate_ids.add(candidate_id)
                canonical_debater = canonical_by_id.get(candidate_id)
                if canonical_debater is None:
                    continue
                canonical_full_norm = " ".join(
                    part
                    for part in [canonical_debater.first_normalized, canonical_debater.last_normalized]
                    if part
                )
                name_similarity = SequenceMatcher(None, full_norm, canonical_full_norm).ratio()
                if name_similarity < 0.76 and not (
                    synthetic_debater.school_id == canonical_debater.school_id and name_similarity >= 0.68
                ):
                    continue
                preliminary_score = (
                    name_similarity * 100
                    + min(max(synthetic_debater.id, canonical_debater.id) / 200, 50)
                    + (30 if synthetic_debater.school_id == canonical_debater.school_id else 0)
                )
                ranked_candidates.append((preliminary_score, name_similarity, canonical_debater))

            ranked_candidates.sort(key=lambda item: item[0], reverse=True)
            for _, name_similarity, canonical_debater in ranked_candidates[:5]:
                pair_key = (synthetic_debater.id, canonical_debater.id)
                candidate_pairs[pair_key] = {
                    "synthetic_debater": synthetic_debater,
                    "canonical_debater": canonical_debater,
                    "name_similarity": name_similarity,
                }

        if not candidate_pairs:
            return []

        ranked_pairs = sorted(
            candidate_pairs.values(),
            key=lambda pair: (
                pair["name_similarity"],
                pair["synthetic_debater"].school_id == pair["canonical_debater"].school_id,
                max(pair["synthetic_debater"].id, pair["canonical_debater"].id),
            ),
            reverse=True,
        )[: self.max_debater_suggestions * 4]

        debaters_to_count = {}
        for pair in ranked_pairs:
            debaters_to_count[pair["synthetic_debater"].id] = pair["synthetic_debater"]
            debaters_to_count[pair["canonical_debater"].id] = pair["canonical_debater"]
        self._attach_total_results(debaters_to_count.values())

        suggestions = []
        for pair in ranked_pairs:
            suggestion = self._create_debater_suggestion(
                pair["synthetic_debater"],
                pair["canonical_debater"],
                pair["name_similarity"],
            )
            if suggestion:
                suggestions.append(suggestion)

        suggestions.sort(key=lambda suggestion: suggestion["score"], reverse=True)
        return suggestions[: self.max_debater_suggestions]

    def _build_school_suggestions(self, selected_school_ids):
        school_queryset = School.all_objects.filter(synthetic=True).order_by("-id")
        if selected_school_ids:
            school_queryset = school_queryset.filter(pk__in=selected_school_ids)
        else:
            school_queryset = school_queryset[: self.default_synthetic_school_limit]
        synthetic_schools = list(school_queryset)
        if not synthetic_schools:
            return []

        canonical_schools = list(School.objects.filter(synthetic=False).order_by("name", "id"))
        if not canonical_schools:
            return []

        canonical_by_id = {}
        canonical_name_groups = defaultdict(list)
        canonical_compact_groups = defaultdict(list)
        canonical_short_groups = defaultdict(list)
        canonical_prefix_groups = defaultdict(list)

        for school in canonical_schools:
            name_normalized, compact_name = self._normalize_school_text(school.name)
            short_normalized, compact_short = self._normalize_school_text(school.short_name)
            school.name_normalized = name_normalized
            school.compact_name = compact_name
            school.short_normalized = short_normalized
            school.compact_short = compact_short
            canonical_by_id[school.id] = school
            canonical_name_groups[name_normalized].append(school.id)
            canonical_compact_groups[compact_name].append(school.id)
            if short_normalized:
                canonical_short_groups[short_normalized].append(school.id)
            if compact_name:
                canonical_prefix_groups[compact_name[:5]].append(school.id)

        suggestions = []
        for synthetic_school in synthetic_schools:
            name_normalized, compact_name = self._normalize_school_text(synthetic_school.name)
            short_normalized, compact_short = self._normalize_school_text(synthetic_school.short_name)

            candidate_ids = []
            candidate_ids.extend(canonical_name_groups.get(name_normalized, []))
            candidate_ids.extend(canonical_compact_groups.get(compact_name, []))
            candidate_ids.extend(canonical_prefix_groups.get(compact_name[:5], []))
            if short_normalized:
                candidate_ids.extend(canonical_short_groups.get(short_normalized, []))
            if compact_short:
                candidate_ids.extend(canonical_short_groups.get(compact_short, []))

            ranked_candidates = []
            seen_candidate_ids = set()
            for candidate_id in candidate_ids:
                if candidate_id in seen_candidate_ids:
                    continue
                seen_candidate_ids.add(candidate_id)
                canonical_school = canonical_by_id.get(candidate_id)
                if canonical_school is None:
                    continue

                name_similarity = SequenceMatcher(
                    None,
                    compact_name or name_normalized,
                    canonical_school.compact_name or canonical_school.name_normalized,
                ).ratio()
                short_similarity = 0
                if short_normalized and canonical_school.short_normalized:
                    short_similarity = SequenceMatcher(
                        None,
                        compact_short or short_normalized,
                        canonical_school.compact_short or canonical_school.short_normalized,
                    ).ratio()

                best_similarity = max(name_similarity, short_similarity)
                same_short_name = bool(
                    short_normalized and short_normalized == canonical_school.short_normalized
                )
                if best_similarity < 0.72 and not same_short_name:
                    continue

                score = best_similarity * 100
                if compact_name == canonical_school.compact_name:
                    score += 25
                if same_short_name:
                    score += 30

                ranked_candidates.append((score, best_similarity, same_short_name, canonical_school))

            ranked_candidates.sort(key=lambda item: item[0], reverse=True)
            for score, best_similarity, same_short_name, canonical_school in ranked_candidates[:3]:
                suggestions.append(
                    {
                        "synthetic_school": synthetic_school,
                        "canonical_school": canonical_school,
                        "name_similarity": best_similarity,
                        "same_short_name": same_short_name,
                        "score": score,
                    }
                )

        suggestions.sort(key=lambda suggestion: suggestion["score"], reverse=True)
        return suggestions[: self.max_school_suggestions]

    def _attach_total_results(self, debaters):
        debaters = list(debaters)
        debater_ids = [debater.id for debater in debaters]
        if not debater_ids:
            return

        totals = defaultdict(int)

        for row in SpeakerResult.objects.filter(debater_id__in=debater_ids).values("debater_id").annotate(
            total=Count("id")
        ):
            totals[row["debater_id"]] += row["total"]

        for row in RoundStats.objects.filter(debater_id__in=debater_ids).values("debater_id").annotate(
            total=Count("id")
        ):
            totals[row["debater_id"]] += row["total"]

        for row in TeamResult.objects.filter(team__debaters__id__in=debater_ids).values(
            "team__debaters"
        ).annotate(total=Count("id", distinct=True)):
            totals[row["team__debaters"]] += row["total"]

        for video_field in ("pm_id", "lo_id", "mg_id", "mo_id"):
            for row in Video.objects.filter(**{f"{video_field}__in": debater_ids}).values(video_field).annotate(
                total=Count("id")
            ):
                totals[row[video_field]] += row["total"]

        for debater in debaters:
            debater.total_results = totals.get(debater.id, 0)

    def _create_debater_suggestion(self, synthetic_debater, canonical_debater, name_similarity):
        if not hasattr(synthetic_debater, "total_results"):
            synthetic_debater.total_results = 0
        if not hasattr(canonical_debater, "total_results"):
            canonical_debater.total_results = 0

        score = self._calculate_merge_score(synthetic_debater, canonical_debater, name_similarity)
        if score <= 0:
            return None

        min_results = min(synthetic_debater.total_results, canonical_debater.total_results)
        max_results = max(synthetic_debater.total_results, canonical_debater.total_results)

        return {
            "synthetic_debater": synthetic_debater,
            "canonical_debater": canonical_debater,
            "score": score,
            "name_similarity": name_similarity,
            "same_school": synthetic_debater.school_id == canonical_debater.school_id,
            "min_results": min_results,
            "max_results": max_results,
        }
