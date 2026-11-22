from dal import autocomplete
from django import forms
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_safe
from django.views.generic import TemplateView

from core.models import Debater, School, Tournament
from core.utils.partnership_network import build_partnership_network, find_network_paths


NETWORK_AXIS_CHOICES = [
    ("", "None"),
    ("toty_points", "TOTY points"),
    ("qual_points", "Qual points"),
    ("first_year", "First year in league"),
    ("final_year", "Final year in league"),
    ("tournaments", "Tournaments attended"),
    ("partner_count", "Partners"),
    ("school", "School"),
    ("qualification_count", "Nationals-qualified seasons"),
]


class PartnershipNetworkForm(forms.Form):
    x_axis = forms.ChoiceField(
        label="X axis", choices=NETWORK_AXIS_CHOICES, required=False,
        widget=forms.Select(attrs={"data-network-axis": "x"}),
    )
    y_axis = forms.ChoiceField(
        label="Y axis", choices=NETWORK_AXIS_CHOICES, required=False,
        widget=forms.Select(attrs={"data-network-axis": "y"}),
    )
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


class NetworkPathForm(PartnershipNetworkForm):
    first_debater = forms.ModelChoiceField(queryset=Debater.objects.all())
    second_debater = forms.ModelChoiceField(queryset=Debater.objects.all())


@require_safe
def partnership_network_paths(request):
    form = NetworkPathForm(request.GET)
    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)
    return JsonResponse({"paths": find_network_paths(
        form.cleaned_data,
        form.cleaned_data["first_debater"],
        form.cleaned_data["second_debater"],
    )})
