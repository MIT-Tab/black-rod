"""Defines the ELO dashboard form, including slider/widget configuration, season-range normalization, and validation for rating parameter increments."""


from django import forms

from core.utils.elo_pipeline import (
    DEFAULT_HIGHER_ELO_LOSS_SHARE,
    DEFAULT_HIGHER_ELO_WIN_SHARE,
    DEFAULT_K_DECAY_SCALE,
    DEFAULT_K_MAX,
    DEFAULT_K_MIN,
    DEFAULT_RATING,
)


class LocalEloDashboardForm(forms.Form):
    DEFAULT_SEASON_MIN = 2017

    season_start = forms.IntegerField(label="Compute Seasons Start")
    season_end = forms.IntegerField(label="Compute Seasons End")
    active_season_start = forms.IntegerField(label="Active Seasons Start")
    active_season_end = forms.IntegerField(label="Active Seasons End")
    k_max = forms.IntegerField(
        initial=int(DEFAULT_K_MAX),
        min_value=5,
        max_value=100,
        label="K Max",
        widget=forms.NumberInput(attrs={"min": "5", "max": "100", "step": "5"}),
    )
    k_min = forms.IntegerField(
        initial=int(DEFAULT_K_MIN),
        min_value=5,
        max_value=100,
        label="K Min",
        widget=forms.NumberInput(attrs={"min": "5", "max": "100", "step": "5"}),
    )
    k_decay_scale = forms.FloatField(initial=DEFAULT_K_DECAY_SCALE, label="K Decay Scale")
    initial_rating = forms.IntegerField(
        initial=int(DEFAULT_RATING),
        min_value=500,
        max_value=2000,
        label="Initial Rating",
        widget=forms.NumberInput(attrs={"min": "500", "max": "2000", "step": "100"}),
    )
    higher_elo_win_share = forms.IntegerField(
        initial=int(DEFAULT_HIGHER_ELO_WIN_SHARE),
        min_value=0,
        max_value=100,
        label="Higher-ELO Win Credit (%)",
        help_text="Percent of team win delta assigned to the higher pre-round ELO partner.",
        widget=forms.NumberInput(
            attrs={
                "type": "range",
                "min": "0",
                "max": "100",
                "step": "5",
                "class": "form-range js-elo-share-slider",
                "data-slider-output-id": "higher-elo-win-share-output",
            }
        ),
    )
    higher_elo_loss_share = forms.IntegerField(
        initial=int(DEFAULT_HIGHER_ELO_LOSS_SHARE),
        min_value=0,
        max_value=100,
        label="Higher-ELO Loss Burden (%)",
        help_text="Percent of team loss delta assigned to the higher pre-round ELO partner.",
        widget=forms.NumberInput(
            attrs={
                "type": "range",
                "min": "0",
                "max": "100",
                "step": "5",
                "class": "form-range js-elo-share-slider",
                "data-slider-output-id": "higher-elo-loss-share-output",
            }
        ),
    )
    min_rounds = forms.IntegerField(initial=0, min_value=0, label="Minimum Inrounds")
    min_outrounds = forms.IntegerField(initial=0, min_value=0, label="Minimum Outrounds")
    seasons = forms.CharField(
        required=False,
        label="Seasons",
        help_text="Comma or newline separated seasons to compute from, e.g. 2024, 2025",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    active_seasons = forms.CharField(
        required=False,
        label="Only Show Active In Seasons",
        help_text="Comma or newline separated seasons to require for display only. Ratings still compute from all included seasons.",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    exclude_proam_partnerships = forms.BooleanField(required=False, initial=False, label="Exclude ProAm Partnerships")
    exclude_dino_rounds = forms.BooleanField(required=False, initial=False, label="Exclude Dino Rounds")

    def __init__(self, *args, season_min=None, season_max=None, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            widget = field.widget
            classes = widget.attrs.get("class", "").strip()
            class_set = {value for value in classes.split() if value}
            if isinstance(widget, forms.CheckboxInput):
                class_set.add("form-check-input")
            elif isinstance(widget, forms.Textarea):
                class_set.update({"form-control", "form-control-sm"})
            elif isinstance(widget, (forms.NumberInput, forms.TextInput, forms.Select)):
                if str(widget.attrs.get("type", "")).lower() == "range":
                    class_set.add("form-range")
                else:
                    class_set.update({"form-control", "form-control-sm"})
            widget.attrs["class"] = " ".join(sorted(class_set))

        resolved_min = int(season_min) if season_min is not None else self.DEFAULT_SEASON_MIN
        resolved_max = int(season_max) if season_max is not None else resolved_min
        resolved_min = max(self.DEFAULT_SEASON_MIN, resolved_min)
        resolved_max = max(self.DEFAULT_SEASON_MIN, resolved_min, resolved_max)

        self.season_min = resolved_min
        self.season_max = resolved_max

        slider_fields = (
            ("season_start", "compute-start"),
            ("season_end", "compute-end"),
            ("active_season_start", "active-start"),
            ("active_season_end", "active-end"),
        )
        for field_name, slider_role in slider_fields:
            self.fields[field_name].widget = forms.NumberInput(
                attrs={
                    "type": "range",
                    "min": str(self.season_min),
                    "max": str(self.season_max),
                    "step": "1",
                    "class": "form-range js-season-bound-slider",
                    "data-slider-role": slider_role,
                }
            )
            self.fields[field_name].min_value = self.season_min
            self.fields[field_name].max_value = self.season_max

        if not self.is_bound:
            self.fields["season_start"].initial = self.season_min
            self.fields["season_end"].initial = self.season_max
            self.fields["active_season_start"].initial = min(
                self.season_max,
                max(self.season_min, self.DEFAULT_SEASON_MIN + 1),
            )
            self.fields["active_season_end"].initial = self.season_max

    def _normalized_range(self, start_key, end_key):
        start = self.cleaned_data.get(start_key)
        end = self.cleaned_data.get(end_key)
        if start is None or end is None:
            return []
        if start > end:
            start, end = end, start
            self.cleaned_data[start_key] = start
            self.cleaned_data[end_key] = end
        return [str(season) for season in range(start, end + 1)]

    def clean(self):
        cleaned_data = super().clean()
        cleaned_data["seasons"] = self._normalized_range("season_start", "season_end")
        cleaned_data["active_seasons"] = self._normalized_range(
            "active_season_start",
            "active_season_end",
        )
        return cleaned_data

    @staticmethod
    def _ensure_increment(value, *, step, label):
        if value % step != 0:
            raise forms.ValidationError(f"{label} must use {step}-point increments.")
        return value

    def clean_higher_elo_win_share(self):
        return self._ensure_increment(
            self.cleaned_data["higher_elo_win_share"],
            step=5,
            label="Higher-ELO Win Credit",
        )

    def clean_higher_elo_loss_share(self):
        return self._ensure_increment(
            self.cleaned_data["higher_elo_loss_share"],
            step=5,
            label="Higher-ELO Loss Burden",
        )

    def clean_k_max(self):
        return self._ensure_increment(
            self.cleaned_data["k_max"],
            step=5,
            label="K Max",
        )

    def clean_k_min(self):
        return self._ensure_increment(
            self.cleaned_data["k_min"],
            step=5,
            label="K Min",
        )

    def clean_initial_rating(self):
        return self._ensure_increment(
            self.cleaned_data["initial_rating"],
            step=100,
            label="Initial Rating",
        )
