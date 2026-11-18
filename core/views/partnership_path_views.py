from dal import autocomplete
from django import forms
from django.contrib.auth.mixins import UserPassesTestMixin
from django.views.generic import TemplateView

from core.models import Debater
from core.utils.partnership_paths import find_partnership_paths


class PartnershipPathForm(forms.Form):
    first_debater = forms.ModelChoiceField(
        queryset=Debater.all_objects.filter(synthetic=False),
        widget=autocomplete.ModelSelect2(
            url="core:debater_autocomplete",
            attrs={
                "data-width": "100%",
                "data-placeholder": "Search by name",
                "data-minimum-input-length": 1,
            },
        ),
    )
    second_debater = forms.ModelChoiceField(
        queryset=Debater.all_objects.filter(synthetic=False),
        widget=autocomplete.ModelSelect2(
            url="core:debater_autocomplete",
            attrs={
                "data-width": "100%",
                "data-placeholder": "Search by name",
                "data-minimum-input-length": 1,
            },
        ),
    )


class PartnershipPathView(UserPassesTestMixin, TemplateView):
    template_name = "admin/partnership_paths.html"

    def test_func(self):
        return self.request.user.is_superuser

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
