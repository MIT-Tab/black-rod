"""Administrative speaker-score statistics."""

from django.contrib.auth.mixins import UserPassesTestMixin
from django.views.generic import TemplateView

from core.utils.speaks_statistics import get_speaks_statistics


class SpeaksStatisticsView(UserPassesTestMixin, TemplateView):
    template_name = "admin/speaks_statistics.html"

    def test_func(self):
        return self.request.user.is_superuser

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["seasons"] = get_speaks_statistics()
        return context
