import logging

from dal import autocomplete
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db import transaction, IntegrityError
from django.db.models import Q
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import FormView, TemplateView

from core.forms import MergeDebaterRequestForm
from core.models import Debater, MergeDebaterRequest, SchoolAdmin
from core.utils.merge import MergeError, get_debater_result_counts, merge_debaters

logger = logging.getLogger(__name__)


def _allowed_merge_seasons():
    return {
        str(settings.CURRENT_SEASON),
        str(int(settings.CURRENT_SEASON) - 1),
    }


def _serialize_merge_request(merge_request):
    primary = merge_request.primary_debater
    secondary = merge_request.secondary_debater

    # Safely get counts - handle None debaters
    primary_counts = get_debater_result_counts(primary) if primary else None
    secondary_counts = get_debater_result_counts(secondary) if secondary else None

    return {
        "instance": merge_request,
        "primary_counts": primary_counts,
        "secondary_counts": secondary_counts,
        "primary_display_name": (
            primary.name if primary else (merge_request.primary_name or "(deleted)")
        ),
        "primary_display_school": (
            primary.school.name
            if primary and primary.school
            else merge_request.primary_school_name
        ),
        "secondary_display_name": (
            secondary.name
            if secondary
            else (merge_request.secondary_name or "(deleted)")
        ),
        "secondary_display_school": (
            secondary.school.name
            if secondary and secondary.school
            else merge_request.secondary_school_name
        ),
    }


class SchoolAdminRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        user = self.request.user
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return SchoolAdmin.objects.filter(user=user).exists()


class MergeDebaterAutocompleteView(
    SchoolAdminRequiredMixin, autocomplete.Select2QuerySetView
):
    paginate_by = 25

    def get_result_label(self, record):
        school_name = record.school.name if record.school else "Unaffiliated"
        return f"<{record.id}> {record.display_name} ({school_name})"

    def get_queryset(self):
        school_id = self.forwarded.get("school")
        if not school_id:
            return Debater.objects.none()

        try:
            school_id = int(school_id)
        except (TypeError, ValueError):
            return Debater.objects.none()

        qs = Debater.objects.select_related("school").filter(
            school_id=school_id,
            latest_season__in=_allowed_merge_seasons(),
        )

        if self.q:
            query_text = str(self.q).strip()
            if query_text.isdigit():
                qs = qs.filter(pk=int(query_text))
            else:
                name_filters = (
                    Q(first_name__icontains=query_text)
                    | Q(last_name__icontains=query_text)
                    | Q(school__name__icontains=query_text)
                )
                for term in query_text.split():
                    name_filters |= (
                        Q(first_name__icontains=term)
                        | Q(last_name__icontains=term)
                        | Q(school__name__icontains=term)
                    )
                qs = qs.filter(name_filters)

        return qs.order_by("last_name", "first_name", "id")


class MergeDebaterRequestCreateView(SchoolAdminRequiredMixin, FormView):
    template_name = "school_admin/merge_debater_request_form.html"
    form_class = MergeDebaterRequestForm
    success_url = reverse_lazy("core:merge_debater_request_create")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        primary = form.cleaned_data["primary_debater"]
        secondary = form.cleaned_data["secondary_debater"]
        MergeDebaterRequest.objects.create(
            requested_by=self.request.user,
            primary_debater=primary,
            secondary_debater=secondary,
            primary_name=primary.name if primary else "",
            primary_school_name=(
                primary.school.name if primary and primary.school else ""
            ),
            secondary_name=secondary.name if secondary else "",
            secondary_school_name=(
                secondary.school.name if secondary and secondary.school else ""
            ),
        )
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
        recent_requests = []
        pending_requests = []

        allowed_seasons = sorted(getattr(form, "allowed_seasons", []), reverse=True)
        debater_one_counts = (
            get_debater_result_counts(debater_one) if debater_one else None
        )
        debater_two_counts = (
            get_debater_result_counts(debater_two) if debater_two else None
        )

        if form:
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

        context.update(
            {
                "debater_one": debater_one,
                "debater_two": debater_two,
                "debater_one_counts": debater_one_counts,
                "debater_two_counts": debater_two_counts,
                "allowed_seasons": allowed_seasons,
                "user_recent_requests": recent_requests,
                "user_pending_requests": pending_requests,
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


class MergeDebaterRequestReviewView(
    LoginRequiredMixin, UserPassesTestMixin, TemplateView
):
    template_name = "admin/merge_debater_requests.html"

    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pending_qs = (
            MergeDebaterRequest.objects.filter(
                status=MergeDebaterRequest.STATUS_PENDING
            )
            .select_related(
                "primary_debater__school",
                "secondary_debater__school",
                "requested_by",
            )
            .order_by("created_at")
        )
        processed_qs = (
            MergeDebaterRequest.objects.exclude(
                status=MergeDebaterRequest.STATUS_PENDING
            )
            .select_related(
                "primary_debater__school",
                "secondary_debater__school",
                "requested_by",
                "processed_by",
            )
            .order_by("-processed_at", "-created_at")[:25]
        )

        context["pending_requests"] = [
            _serialize_merge_request(obj) for obj in pending_qs
        ]
        context["processed_requests"] = [
            _serialize_merge_request(obj) for obj in processed_qs
        ]
        return context

    def post(self, request, *args, **kwargs):
        request_id = request.POST.get("request_id")
        action = request.POST.get("action")

        if not request_id or action not in {"approve", "deny", "swap"}:
            messages.error(request, "Invalid request submission.")
            return redirect(self.get_success_url())

        # Handle swap action separately
        if action == "swap":
            try:
                merge_request = MergeDebaterRequest.objects.select_related(
                    "primary_debater__school", "secondary_debater__school"
                ).get(pk=request_id)

                if not merge_request.is_pending:
                    messages.warning(
                        request, "This merge request has already been processed."
                    )
                    return redirect(self.get_success_url())

                # Swap the debaters
                primary_debater = merge_request.primary_debater
                primary_name = merge_request.primary_name
                primary_school_name = merge_request.primary_school_name

                merge_request.primary_debater = merge_request.secondary_debater
                merge_request.primary_name = merge_request.secondary_name
                merge_request.primary_school_name = merge_request.secondary_school_name

                merge_request.secondary_debater = primary_debater
                merge_request.secondary_name = primary_name
                merge_request.secondary_school_name = primary_school_name

                merge_request.save()

                messages.success(request, "Debaters swapped successfully.")
            except MergeDebaterRequest.DoesNotExist:
                messages.error(request, "Merge request could not be found.")

            return redirect(self.get_success_url())

        # Fetch the merge request first, outside of transaction
        try:
            merge_request = MergeDebaterRequest.objects.select_related(
                "primary_debater__school", "secondary_debater__school"
            ).get(pk=request_id)
        except MergeDebaterRequest.DoesNotExist:
            messages.error(request, "Merge request could not be found.")
            return redirect(self.get_success_url())

        if not merge_request.is_pending:
            messages.warning(request, "This merge request has already been processed.")
            return redirect(self.get_success_url())

        # Cache names before transaction in case objects get modified
        primary_name = (
            merge_request.primary_debater.name
            if merge_request.primary_debater
            else "Unknown"
        )
        secondary_name = (
            merge_request.secondary_debater.name
            if merge_request.secondary_debater
            else "Unknown"
        )

        try:
            with transaction.atomic():
                # Re-fetch with lock inside transaction
                merge_request = MergeDebaterRequest.objects.select_for_update().get(
                    pk=request_id
                )

                if action == "approve":
                    # Perform the merge (this may raise exceptions)
                    merge_debaters(
                        merge_request.primary_debater, merge_request.secondary_debater
                    )
                    merge_request.refresh_from_db()
                    merge_request.secondary_debater = None

                    merge_request.status = MergeDebaterRequest.STATUS_APPROVED
                    merge_request.denial_reason = ""
                    success_message = f"Merged {secondary_name} into {primary_name}."
                else:
                    reason = request.POST.get("denial_reason", "").strip()
                    merge_request.status = MergeDebaterRequest.STATUS_DENIED
                    merge_request.denial_reason = reason
                    success_message = "Merge request denied."

                # Update name fields from current debater objects
                primary_obj = merge_request.primary_debater
                if primary_obj:
                    merge_request.primary_name = primary_obj.name
                    merge_request.primary_school_name = (
                        primary_obj.school.name
                        if primary_obj.school
                        else merge_request.primary_school_name
                    )

                secondary_obj = merge_request.secondary_debater
                if secondary_obj:
                    merge_request.secondary_name = secondary_obj.name
                    merge_request.secondary_school_name = (
                        secondary_obj.school.name
                        if secondary_obj.school
                        else merge_request.secondary_school_name
                    )

                # Save the changes
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
                MergeDebaterRequest.objects.filter(pk=merge_request.pk).update(
                    **update_data
                )

            # Transaction completed successfully - we're now outside the atomic block
            if action == "approve":
                messages.success(request, success_message)
            else:
                messages.info(request, success_message)

        except MergeError as exc:
            # Transaction will auto-rollback
            error_msg = str(exc)

            # Provide more user-friendly messages for common errors
            if "primary key" in error_msg.lower():
                user_message = "Merge failed: One of the selected debaters no longer exists. Please refresh the page and try again."
            elif "cannot merge a debater into itself" in error_msg.lower():
                user_message = "Merge failed: Cannot merge a debater into themselves."
            else:
                user_message = f"Merge failed: {error_msg}"

            messages.error(request, user_message)

            # Log to Sentry with additional context
            logger.error(
                f"MergeError during debater merge request #{request_id}",
                exc_info=True,
                extra={
                    "request_id": request_id,
                    "primary_debater": primary_name,
                    "secondary_debater": secondary_name,
                    "user": request.user.username,
                    "error_type": "MergeError",
                },
            )

        except IntegrityError as exc:
            # Transaction will auto-rollback
            error_msg = str(exc)

            # Check for common database constraint violations
            if "unique" in error_msg.lower() or "duplicate" in error_msg.lower():
                user_message = (
                    "Merge failed: A database constraint was violated. This usually means "
                    "the debaters have conflicting records that cannot be automatically merged. "
                    "Please contact a technical administrator for assistance."
                )
            elif "foreign key" in error_msg.lower():
                user_message = (
                    "Merge failed: Database relationship error. "
                    "Please contact a technical administrator for assistance."
                )
            else:
                user_message = (
                    "Merge failed: A database error occurred. "
                    "Please contact a technical administrator for assistance."
                )

            messages.error(request, user_message)

            # Log to Sentry with full context
            logger.error(
                f"IntegrityError during debater merge request #{request_id}",
                exc_info=True,
                extra={
                    "request_id": request_id,
                    "primary_debater": primary_name,
                    "secondary_debater": secondary_name,
                    "user": request.user.username,
                    "error_type": "IntegrityError",
                    "db_error": error_msg,
                },
            )

        except Exception as exc:
            # Transaction will auto-rollback
            error_msg = str(exc)
            error_type = type(exc).__name__

            # Generic user-facing message
            user_message = (
                "An unexpected error occurred while processing the merge request. "
                "The merge has been cancelled and no changes were made. "
                "Please contact a technical administrator if this problem persists."
            )

            messages.error(request, user_message)

            # Log full details to Sentry
            logger.error(
                f"Unexpected error during debater merge request #{request_id}",
                exc_info=True,
                extra={
                    "request_id": request_id,
                    "primary_debater": primary_name,
                    "secondary_debater": secondary_name,
                    "user": request.user.username,
                    "error_type": error_type,
                    "error_message": error_msg,
                },
            )

        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse_lazy("core:merge_debater_request_review")
