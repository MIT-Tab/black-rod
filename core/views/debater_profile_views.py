from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView

from core.models import Debater, ClaimDebaterRequest


class MyDebaterProfileView(LoginRequiredMixin, TemplateView):
    """View for users to see their claimed debater profiles and claim requests."""
    template_name = "debater/my_profile.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Get user's claimed debater profiles
        claimed_debaters = Debater.objects.filter(
            user=self.request.user
        ).select_related('school').order_by('-latest_season')

        # Get user's claim requests
        claim_requests = (
            ClaimDebaterRequest.objects.filter(requested_by=self.request.user)
            .select_related("debater__school", "processed_by")
            .order_by("-created_at")[:20]
        )

        pending_requests = []
        recent_requests = []

        for request_obj in claim_requests:
            if request_obj.is_pending:
                pending_requests.append(request_obj)
            else:
                recent_requests.append(request_obj)

        context.update({
            "claimed_debaters": claimed_debaters,
            "pending_requests": pending_requests,
            "recent_requests": recent_requests,
        })

        return context
