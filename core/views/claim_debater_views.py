from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.cache import cache
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import FormView, TemplateView, UpdateView

from core.forms import ClaimDebaterRequestForm, DebaterProfileEditForm
from core.models import ClaimDebaterRequest, Debater


class ClaimDebaterRequestCreateView(LoginRequiredMixin, FormView):
    template_name = "debater/claim_debater_request_form.html"
    form_class = ClaimDebaterRequestForm
    success_url = reverse_lazy("core:my_debater_profile")

    RATE_LIMIT = 5  # Maximum requests per day
    RATE_LIMIT_WINDOW = 86400  # 24 hours in seconds

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        if self._is_rate_limited():
            form.add_error(
                None,
                "You have reached the daily limit for claim requests. "
                "Please try again tomorrow or contact an administrator.",
            )
            return self.form_invalid(form)

        debater = form.cleaned_data["debater"]
        ClaimDebaterRequest.objects.create(
            requested_by=self.request.user,
            debater=debater,
        )
        self._increment_rate_limit()

        messages.success(
            self.request,
            "Claim request submitted. A site administrator will review it shortly.",
        )
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["rate_limit_reached"] = self._is_rate_limited()
        return context

    def _rate_limit_cache_key(self):
        return f"claim_debater_rate_limit:{self.request.user.id}"

    def _is_rate_limited(self):
        cache_key = self._rate_limit_cache_key()
        count = cache.get(cache_key, 0)
        return count >= self.RATE_LIMIT

    def _increment_rate_limit(self):
        cache_key = self._rate_limit_cache_key()
        count = cache.get(cache_key, 0)
        cache.set(cache_key, count + 1, self.RATE_LIMIT_WINDOW)


class ClaimDebaterRequestReviewView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "admin/claim_debater_requests.html"

    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        pending_qs = (
            ClaimDebaterRequest.objects.filter(status=ClaimDebaterRequest.STATUS_PENDING)
            .select_related("debater__school", "requested_by")
            .order_by("created_at")
        )

        processed_qs = (
            ClaimDebaterRequest.objects.exclude(status=ClaimDebaterRequest.STATUS_PENDING)
            .select_related("debater__school", "requested_by", "processed_by")
            .order_by("-processed_at", "-created_at")[:25]
        )

        context["pending_requests"] = pending_qs
        context["processed_requests"] = processed_qs
        return context

    def post(self, request, *args, **kwargs):
        request_id = request.POST.get("request_id")
        action = request.POST.get("action")

        if not request_id or action not in {"approve", "deny"}:
            messages.error(request, "Invalid request submission.")
            return redirect(self.get_success_url())

        with transaction.atomic():
            claim_request = (
                ClaimDebaterRequest.objects.select_for_update()
                .select_related("debater")
                .filter(pk=request_id)
                .first()
            )

            if not claim_request:
                messages.error(request, "Claim request could not be found.")
                return redirect(self.get_success_url())

            if not claim_request.is_pending:
                messages.warning(request, "This claim request has already been processed.")
                return redirect(self.get_success_url())

            if action == "approve":
                debater = claim_request.debater

                # Check if debater is already claimed
                if debater.user:
                    messages.error(
                        request,
                        f"{debater.name} has already been claimed by another user."
                    )
                    return redirect(self.get_success_url())

                # Assign the debater to the user
                debater.user = claim_request.requested_by
                debater.save()

                claim_request.status = ClaimDebaterRequest.STATUS_APPROVED
                messages.success(
                    request,
                    f"Approved claim request. {debater.name} is now linked to {claim_request.requested_by.username}.",
                )
            else:
                reason = request.POST.get("denial_reason", "").strip()
                claim_request.status = ClaimDebaterRequest.STATUS_DENIED
                claim_request.denial_reason = reason
                messages.info(request, "Claim request denied.")

            claim_request.processed_by = request.user
            claim_request.processed_at = timezone.now()
            claim_request.save()

        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse_lazy("core:claim_debater_request_review")


class DebaterProfileEditView(LoginRequiredMixin, UpdateView):
    model = Debater
    form_class = DebaterProfileEditForm
    template_name = "debater/debater_profile_edit.html"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)

        # Check if the current user owns this debater profile
        if obj.user != self.request.user and not self.request.user.is_superuser:
            raise Http404("You do not have permission to edit this profile.")

        return obj

    def form_valid(self, form):
        messages.success(self.request, "Profile updated successfully!")
        return super().form_valid(form)

    def get_success_url(self):
        return self.object.get_absolute_url()
