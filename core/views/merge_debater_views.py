from copy import deepcopy

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.cache import cache
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import FormView, TemplateView

from core.forms import MergeDebaterRequestForm
from core.models import Debater, MergeDebaterRequest, SchoolAdmin
from core.utils.merge import MergeError, get_debater_result_counts, merge_debaters


def _serialize_merge_request(merge_request):
    primary = merge_request.primary_debater
    secondary = merge_request.secondary_debater
    primary_counts = get_debater_result_counts(primary)
    secondary_counts = get_debater_result_counts(secondary)
    return {
        "instance": merge_request,
        "primary_counts": primary_counts,
        "secondary_counts": secondary_counts,
        "primary_display_name": primary.name if primary else (merge_request.primary_name or "(deleted)"),
        "primary_display_school": (primary.school.name if primary and primary.school else merge_request.primary_school_name),
        "secondary_display_name": secondary.name if secondary else (merge_request.secondary_name or "(deleted)"),
        "secondary_display_school": (secondary.school.name if secondary and secondary.school else merge_request.secondary_school_name),
    }


class SchoolAdminRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        user = self.request.user
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return SchoolAdmin.objects.filter(user=user).exists()


class MergeDebaterRequestCreateView(SchoolAdminRequiredMixin, FormView):
    template_name = "school_admin/merge_debater_request_form.html"
    form_class = MergeDebaterRequestForm
    success_url = reverse_lazy("core:merge_debater_request_create")

    RATE_LIMIT = 5

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        if self._is_rate_limited():
            form.add_error(
                None,
                "You have reached the daily limit for merge requests. "
                "Please try again tomorrow or contact an administrator.",
            )
            return self.form_invalid(form)

        primary = form.cleaned_data["primary_debater"]
        secondary = form.cleaned_data["secondary_debater"]
        MergeDebaterRequest.objects.create(
            requested_by=self.request.user,
            primary_debater=primary,
            secondary_debater=secondary,
            primary_name=primary.name if primary else "",
            primary_school_name=primary.school.name if primary and primary.school else "",
            secondary_name=secondary.name if secondary else "",
            secondary_school_name=secondary.school.name if secondary and secondary.school else "",
        )
        self._increment_rate_limit()

        messages.success(
            self.request,
            "Merge request submitted. A site administrator will review it shortly.",
        )
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context.get("form")

        debater_one = self._selected_debater(form, "debater_one")
        debater_two = self._selected_debater(form, "debater_two")

        school_one_debaters = []
        school_two_debaters = []
        recent_requests = []
        pending_requests = []

        allowed_seasons = sorted(getattr(form, "allowed_seasons", []), reverse=True)

        debater_data_cache = {}

        def serialize_debater_obj(debater):
            if not debater:
                return None
            cached = debater_data_cache.get(debater.pk)
            if cached is None:
                counts = get_debater_result_counts(debater) or {}
                debater_data_cache[debater.pk] = {
                    "id": debater.id,
                    "name": debater.name,
                    "school": debater.school.name if debater.school else "",
                    "school_id": debater.school_id,
                    "counts": {
                        "team_results": counts.get("team_results", 0),
                        "speaker_results": counts.get("speaker_results", 0),
                        "round_stats": counts.get("round_stats", 0),
                        "videos": counts.get("videos", 0),
                        "total": counts.get("total", 0),
                    },
                }
            return deepcopy(debater_data_cache[debater.pk])

        debater_one_data = serialize_debater_obj(debater_one)
        debater_two_data = serialize_debater_obj(debater_two)

        if form:
            admin_school_ids = list(
                form.fields["school_one"].queryset.values_list("id", flat=True)
            )
            active_school_ids = list(
                form.fields["school_two"].queryset.values_list("id", flat=True)
            )

            if admin_school_ids:
                school_one_qs = (
                    Debater.objects.filter(
                        school_id__in=admin_school_ids,
                        latest_season__in=allowed_seasons,
                    )
                    .select_related("school")
                    .order_by("school__name", "last_name", "first_name")
                )
                school_one_debaters = [serialize_debater_obj(debater) for debater in school_one_qs]

            if active_school_ids:
                school_two_qs = (
                    Debater.objects.filter(
                        school_id__in=active_school_ids,
                        latest_season__in=allowed_seasons,
                    )
                    .select_related("school")
                    .order_by("school__name", "last_name", "first_name")
                )
                school_two_debaters = [serialize_debater_obj(debater) for debater in school_two_qs]

            user_requests = (
                MergeDebaterRequest.objects.filter(requested_by=self.request.user)
                .select_related(
                    "primary_debater__school",
                    "secondary_debater__school",
                    "processed_by",
                )
                .order_by("-created_at")[:10]
            )

            for request_obj in user_requests:
                serialized = _serialize_merge_request(request_obj)
                if request_obj.is_pending:
                    pending_requests.append(serialized)
                else:
                    recent_requests.append(serialized)

        debater_data_json = {str(key): value for key, value in debater_data_cache.items()}

        context.update(
            {
                "debater_one": debater_one,
                "debater_two": debater_two,
                "debater_one_counts": debater_one_data["counts"] if debater_one_data else None,
                "debater_two_counts": debater_two_data["counts"] if debater_two_data else None,
                "allowed_seasons": allowed_seasons,
                "school_one_debaters": school_one_debaters,
                "school_two_debaters": school_two_debaters,
                "user_recent_requests": recent_requests,
                "user_pending_requests": pending_requests,
                "debater_data_map": debater_data_json,
            }
        )
        return context

    def _selected_debater(self, form, field_name):
        if not form:
            return None
        data_source = form.data if form.is_bound else form.initial
        debater_id = data_source.get(field_name)
        if not debater_id:
            return None
        try:
            return Debater.objects.select_related("school").get(pk=int(debater_id))
        except (ValueError, Debater.DoesNotExist):
            return None

    def _rate_limit_cache_key(self):
        today = timezone.now().date().isoformat()
        return f"merge_request_limit_{self.request.user.pk}_{today}"

    def _is_rate_limited(self):
        return cache.get(self._rate_limit_cache_key(), 0) >= self.RATE_LIMIT

    def _increment_rate_limit(self):
        key = self._rate_limit_cache_key()
        count = cache.get(key, 0)
        cache.set(key, count + 1, 86400)


class MergeDebaterRequestReviewView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "admin/merge_debater_requests.html"

    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pending_qs = (
            MergeDebaterRequest.objects.filter(status=MergeDebaterRequest.STATUS_PENDING)
            .select_related(
                "primary_debater__school",
                "secondary_debater__school",
                "requested_by",
            )
            .order_by("created_at")
        )
        processed_qs = (
            MergeDebaterRequest.objects.exclude(status=MergeDebaterRequest.STATUS_PENDING)
            .select_related(
                "primary_debater__school",
                "secondary_debater__school",
                "requested_by",
                "processed_by",
            )
            .order_by("-processed_at", "-created_at")[:25]
        )

        context["pending_requests"] = [_serialize_merge_request(obj) for obj in pending_qs]
        context["processed_requests"] = [_serialize_merge_request(obj) for obj in processed_qs]
        return context

    def post(self, request, *args, **kwargs):
        request_id = request.POST.get("request_id")
        action = request.POST.get("action")

        if not request_id or action not in {"approve", "deny"}:
            messages.error(request, "Invalid request submission.")
            return redirect(self.get_success_url())

        with transaction.atomic():
            merge_request = (
                MergeDebaterRequest.objects.select_for_update()
                .select_related("primary_debater", "secondary_debater")
                .filter(pk=request_id)
                .first()
            )

            if not merge_request:
                messages.error(request, "Merge request could not be found.")
                return redirect(self.get_success_url())

            if not merge_request.is_pending:
                messages.warning(request, "This merge request has already been processed.")
                return redirect(self.get_success_url())

            if action == "approve":
                primary_name = merge_request.primary_debater.name if merge_request.primary_debater else "Unknown"
                secondary_name = merge_request.secondary_debater.name if merge_request.secondary_debater else "Unknown"
                try:
                    merge_debaters(merge_request.primary_debater, merge_request.secondary_debater)
                    merge_request.refresh_from_db()
                    merge_request.secondary_debater = None
                except MergeError as exc:
                    messages.error(request, f"Merge failed: {exc}")
                    return redirect(self.get_success_url())
                except Exception as exc:  # pragma: no cover - safeguard
                    messages.error(request, f"Unexpected error: {exc}")
                    return redirect(self.get_success_url())

                merge_request.status = MergeDebaterRequest.STATUS_APPROVED
                merge_request.denial_reason = ""
                messages.success(
                    request,
                    f"Merged {secondary_name} into {primary_name}.",
                )
            else:
                reason = request.POST.get("denial_reason", "").strip()
                merge_request.status = MergeDebaterRequest.STATUS_DENIED
                merge_request.denial_reason = reason
                messages.info(request, "Merge request denied.")

            primary_obj = merge_request.primary_debater
            if primary_obj:
                merge_request.primary_name = primary_obj.name
                merge_request.primary_school_name = (
                    primary_obj.school.name if primary_obj.school else merge_request.primary_school_name
                )

            secondary_obj = merge_request.secondary_debater
            if secondary_obj:
                merge_request.secondary_name = secondary_obj.name
                merge_request.secondary_school_name = (
                    secondary_obj.school.name if secondary_obj.school else merge_request.secondary_school_name
                )

            update_data = {
                "status": merge_request.status,
                "denial_reason": merge_request.denial_reason,
                "processed_by": request.user,
                "processed_at": timezone.now(),
                "primary_name": merge_request.primary_name,
                "primary_school_name": merge_request.primary_school_name,
                "secondary_name": merge_request.secondary_name,
                "secondary_school_name": merge_request.secondary_school_name,
                "secondary_debater_id": merge_request.secondary_debater_id,
            }
            MergeDebaterRequest.objects.filter(pk=merge_request.pk).update(**update_data)
            merge_request.refresh_from_db()

        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse_lazy("core:merge_debater_request_review")
