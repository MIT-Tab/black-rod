"""Admin-only, in-memory DOTY leaderboard preview."""

from django.conf import settings
from django.contrib.auth.mixins import UserPassesTestMixin
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.generic import TemplateView

from core.models import Tournament
from core.utils.doty import get_doty_variants


@method_decorator(never_cache, name="dispatch")
class DOTYPreviewView(UserPassesTestMixin, TemplateView):
    template_name = "admin/doty.html"

    def test_func(self):
        return self.request.user.is_superuser

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        seasons = list(Tournament.objects.order_by("-season").values_list(
            "season", flat=True
        ).distinct())
        season = self.request.GET.get("season")
        if season not in seasons:
            season = (settings.CURRENT_SEASON if settings.CURRENT_SEASON in seasons
                      else next(iter(seasons), None))
        context["seasons"] = [
            {"value": value, "label": Tournament(season=value).get_season_display()}
            for value in seasons
        ]
        context["season"] = season
        context["season_display"] = Tournament(season=season).get_season_display() if season else ""
        variants = get_doty_variants(season) if season else {}
        context["standings"] = variants.get("combined", [])
        context["leaderboards"] = [
            {
                "id": key,
                "title": title,
                "description": description,
                "separate": key != "combined",
                "standings": variants.get(key, []),
                "slots": range(1, limit + 1),
                "colspan": limit + 4,
            }
            for key, title, description, limit in (
                ("combined", "Original: 6 combined markers",
                 "The best six tournament totals, each adding SOTY + qual points.", 6),
                ("separate_9", "Alternative: 9 separate markers",
                 "The best nine individual SOTY or qual markers across all tournaments.", 9),
                ("separate_12", "Alternative: 12 separate markers",
                 "The best twelve individual SOTY or qual markers across all tournaments.", 12),
            )
        ]
        return context
