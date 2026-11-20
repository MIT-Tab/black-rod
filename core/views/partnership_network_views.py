from dal import autocomplete
from django import forms
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_safe
from django.views.generic import TemplateView

from core.models import School, Tournament
from core.utils.partnership_network import build_partnership_network


class PartnershipNetworkForm(forms.Form):
    schools = forms.ModelMultipleChoiceField(
        queryset=School.objects.order_by("name"),
        required=False,
        widget=autocomplete.ModelSelect2Multiple(
            url="core:school_autocomplete",
            attrs={"data-width": "100%", "data-placeholder": "All schools"},
        ),
    )
    from_season = forms.ChoiceField(label="From season", required=False)
    to_season = forms.ChoiceField(label="Through season", required=False)
    qualified = forms.BooleanField(label="Nationals qualified", required=False)
    min_tournaments = forms.IntegerField(
        label="Minimum tournaments", min_value=0, required=False,
        widget=forms.NumberInput(attrs={"placeholder": "0"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        seasons = Tournament.objects.order_by("season").values_list("season", flat=True).distinct()
        choices = [("", "All seasons")] + [(season, season) for season in seasons]
        self.fields["from_season"].choices = choices
        self.fields["to_season"].choices = choices

    def clean(self):
        cleaned = super().clean()
        first, last = cleaned.get("from_season"), cleaned.get("to_season")
        if first and last and first > last:
            self.add_error("to_season", "Choose a season on or after the starting season.")
        return cleaned


class PartnershipNetworkView(TemplateView):
    template_name = "debaters/partnership_network.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = PartnershipNetworkForm(self.request.GET)
        context["form"] = form
        if form.is_valid():
            context["network_url"] = (
                reverse("core:partnership_network_data") + "?" + self.request.GET.urlencode()
            )
        return context


@require_safe
def partnership_network_data(request):
    form = PartnershipNetworkForm(request.GET)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)
    return JsonResponse(build_partnership_network(form.cleaned_data))
