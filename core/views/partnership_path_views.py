from dal import autocomplete
from django import forms
from django.core.cache import cache
from django.db.models import Case, Value, When
from django.db.models.functions import Concat, Lower, Trim
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe
from django.views.generic import TemplateView

from core.models import Debater
from core.utils.partnership_paths import find_partnership_paths
from core.utils.partnership_stats import calculate_partnership_stats

STATS_CACHE_KEY = "public-partnership-stats:v2"
STATS_CACHE_TIMEOUT = 24 * 60 * 60
STATS_LOCK_KEY = f"{STATS_CACHE_KEY}:lock"


class PartnershipDebaterAutocomplete(autocomplete.Select2QuerySetView):
    def get_queryset(self):
        words = (self.q or "").split()
        debaters = Debater.objects.select_related("school")
        if not words:
            return debaters.none()

        query = " ".join(words)
        matches = debaters.alias(full_name=Trim(Concat("first_name", Value(" "), "last_name")))
        for word in words:
            matches = matches.filter(full_name__icontains=word)

        return matches.order_by(
            Case(
                When(full_name__iexact=query, then=Value(0)),
                When(full_name__istartswith=query, then=Value(1)),
                default=Value(2),
            ),
            Lower("full_name"),
            "pk",
        )

    def get_result_label(self, result):
        school = result.school.name if result.school else "Unaffiliated"
        return f"{result.name} ({school}) · #{result.pk}"


class PartnershipDebaterField(forms.ModelChoiceField):
    def __init__(self):
        super().__init__(
            queryset=Debater.objects.all(),
            widget=autocomplete.ModelSelect2(
                url="core:partnership_debater_autocomplete",
                attrs={
                    "data-width": "100%",
                    "data-placeholder": "Search by name",
                    "data-minimum-input-length": 1,
                },
            ),
        )


class PartnershipPathForm(forms.Form):
    first_debater = PartnershipDebaterField()
    second_debater = PartnershipDebaterField()


class PartnershipPathView(TemplateView):
    template_name = "debaters/partnership_paths.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = PartnershipPathForm(self.request.GET or None)
        context["form"] = form
        if form.is_valid():
            context["searched"] = True
            context["paths"] = find_partnership_paths(
                form.cleaned_data["first_debater"], form.cleaned_data["second_debater"]
            )
        return context


class PartnershipStatsView(TemplateView):
    template_name = "debaters/partnership_stats.html"


@never_cache
@require_safe
def partnership_stats_results(_request):
    html = cache.get(STATS_CACHE_KEY)
    if html is not None:
        return HttpResponse(html)
    if not cache.add(STATS_LOCK_KEY, True, timeout=120):
        return HttpResponse(status=202)

    try:
        html = cache.get(STATS_CACHE_KEY)
        if html is None:
            html = render_to_string(
                "debaters/_partnership_stats_results.html", calculate_partnership_stats()
            )
            cache.set(STATS_CACHE_KEY, html, timeout=STATS_CACHE_TIMEOUT)
        return HttpResponse(html)
    finally:
        cache.delete(STATS_LOCK_KEY)
