from django import forms

from core.utils.scheduler import DEFAULT_SCHEDULER_SETTINGS, merge_scheduler_settings


class SchedulerVersionedForm(forms.Form):
    workspace_version = forms.IntegerField(widget=forms.HiddenInput())


class SchedulerSettingsForm(SchedulerVersionedForm):
    max_workers = forms.IntegerField(
        min_value=1,
        max_value=64,
        label="Parallel workers",
        help_text="How many scenario checks to run at the same time.",
    )
    already_scheduled_penalty = forms.IntegerField(label="Already scheduled score")
    rank_1_penalty = forms.IntegerField(label="Rank 1 score")
    rank_2_penalty = forms.IntegerField(label="Rank 2 score")
    rank_3_penalty = forms.IntegerField(label="Rank 3 score")
    impossible_penalty = forms.IntegerField(label="Impossible score")
    missing_unopposed_host_penalty = forms.IntegerField(
        label="Missing unopposed host score"
    )
    missing_requested_unopposed_penalty = forms.IntegerField(
        label="Missing requested unopposed score"
    )
    missing_tag_penalty = forms.IntegerField(label="Missing tag score")
    tag_bonus = forms.IntegerField(label="Tag match bonus")
    north_to_south_penalty = forms.IntegerField(label="North school in South slot")
    north_to_central_penalty = forms.IntegerField(
        label="North school in Central slot"
    )
    south_to_central_penalty = forms.IntegerField(
        label="South school in Central slot"
    )
    central_to_north_penalty = forms.IntegerField(
        label="Central school in North slot"
    )
    central_to_south_penalty = forms.IntegerField(
        label="Central school in South slot"
    )
    central_on_two_tournament_weekend_penalty = forms.IntegerField(
        label="Central school on 2-tournament weekend"
    )
    south_to_north_penalty = forms.IntegerField(label="South school in North slot")

    def __init__(self, *args, settings_data=None, **kwargs):
        super().__init__(*args, **kwargs)
        merged_settings = merge_scheduler_settings(settings_data)
        for field_name in DEFAULT_SCHEDULER_SETTINGS:
            self.fields[field_name].initial = merged_settings[field_name]
            self.fields[field_name].widget.attrs.setdefault("class", "form-control")


class SchedulerCSVUploadForm(SchedulerVersionedForm):
    schools_csv = forms.FileField(
        required=False,
        label="Schools CSV",
        help_text="Upload the spreadsheet that lists schools, priorities, tags, and preferences.",
    )
    dates_csv = forms.FileField(
        required=False,
        label="Dates CSV",
        help_text="Upload the spreadsheet that lists dates, weekend counts, and tags.",
    )

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("schools_csv") and not cleaned.get("dates_csv"):
            raise forms.ValidationError("Upload at least one CSV file.")
        return cleaned

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["schools_csv"].widget.attrs.setdefault("class", "form-control-file")
        self.fields["dates_csv"].widget.attrs.setdefault("class", "form-control-file")
