from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Div, Layout, Row, Submit
from dal import autocomplete
from django import forms
from django.conf import settings
from django.core.validators import URLValidator
from django.db.models import Q
from django.forms import formset_factory
from django_summernote.widgets import SummernoteInplaceWidget

from core.models import Team, TOTYReaff
from core.models.debater import Debater, QualPoints, Reaff
from core.models.merge_request import MergeDebaterRequest
from core.models.school import School
from core.models.school_admin import SchoolAdmin
from core.models.standings.coty import COTY
from core.models.standings.noty import NOTY
from core.models.standings.qual import QUAL
from core.models.standings.soty import SOTY
from core.models.tournament import Tournament
from core.models.video import Video


class SchoolForm(forms.ModelForm):
    existing_school = forms.ModelChoiceField(
        queryset=School.objects.all(),
        required=False,
        label="Link to Existing School",
        help_text="If this school already exists under a different name, select it here instead of creating a new one",
        widget=autocomplete.ModelSelect2(url="core:school_autocomplete"),
    )

    class Meta:
        model = School
        fields = ("name", "included_in_oty")


class DebaterForm(forms.ModelForm):
    school = forms.ModelChoiceField(
        queryset=School.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:school_autocomplete"),
    )
    tournament_id = forms.CharField(widget=forms.HiddenInput(), required=False)
    last_name = forms.CharField(max_length=32, required=False)

    class Meta:
        model = Debater
        fields = ("first_name", "last_name", "school")


class VideoForm(forms.ModelForm):
    class Meta:
        model = Video

        fields = (
            "pm",
            "mg",
            "lo",
            "mo",
            "tournament",
            "round",
            "case",
            "description",
            "link",
            "password",
            "permissions",
            "tags",
        )

        widgets = {
            "pm": autocomplete.ModelSelect2(url="core:debater_autocomplete"),
            "lo": autocomplete.ModelSelect2(url="core:debater_autocomplete"),
            "mg": autocomplete.ModelSelect2(url="core:debater_autocomplete"),
            "mo": autocomplete.ModelSelect2(url="core:debater_autocomplete"),
            "tournament": autocomplete.ModelSelect2(
                url="core:all_tournament_autocomplete"
            ),
            "case": SummernoteInplaceWidget(),
            "description": SummernoteInplaceWidget(),
            "tags": autocomplete.TaggitSelect2("core:tag_autocomplete"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.layout = Layout(
            Row(
                Column("tournament", css_class="col-md-6"),
                Column("round", css_class="col-md-6"),
            ),
            Div(css_class="border-top my-3"),
            Row(
                Column("pm", "mg", css_class="col-md-6"),
                Column("lo", "mo", css_class="col-md-6"),
            ),
            Div(css_class="border-top my-3"),
            Row(
                Column("link", css_class="col-md-4"),
                Column("password", css_class="col-md-4"),
                Column("permissions", css_class="col-md-4"),
            ),
            Div(css_class="border-top my-3"),
            Row("case", "description", "tags"),
            Submit("Create", "Create"),
        )


class TournamentForm(forms.ModelForm):
    host = forms.ModelChoiceField(
        queryset=School.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:school_autocomplete"),
    )

    season = forms.ChoiceField(choices=settings.SEASONS, widget=forms.Select())

    class Meta:
        model = Tournament
        fields = (
            "host",
            "season",
            "date",
            "num_teams",
            "num_novice_debaters",
            "qual_type",
            "name_suffix",
            "manual_name",
        )


class TournamentCreateForm(TournamentForm):
    api_url = forms.URLField(
        required=False,
        label="API URL (Optional)",
        help_text="If provided, results will be automatically imported from this URL",
        widget=forms.URLInput(attrs={'placeholder': 'https://nu-tab.com/tournament/123'})
    )


class TeamForm(forms.ModelForm):
    debaters = forms.ModelMultipleChoiceField(
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2Multiple(url="core:debater_autocomplete"),
    )

    class Meta:
        model = Team
        fields = ("debaters",)

    def clean(self):
        cleaned_data = super().clean()

        if not len(cleaned_data.get("debaters")) == 2:
            raise forms.ValidationError("All teams must have 2 debaters")


class TournamentDetailForm(forms.Form):
    num_teams = forms.IntegerField(label="Number of teams")
    num_novices = forms.IntegerField(label="Number of novices")


class TournamentImportForm(forms.Form):
    url = forms.CharField(
        label="URL",
        help_text='Please enter the URL for the tournament without any trailing \
            slashes but including http://.  For example: "http://mit.nu-tab.com"',
        validators=[URLValidator()],
    )

class TeamResultForm(forms.Form):
    debater_one = forms.ModelChoiceField(
        label="Debater One",
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
        required=False,
    )

    debater_two = forms.ModelChoiceField(
        label="Debater Two",
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
        required=False,
    )

    ghost_points = forms.BooleanField(label="Ghost Points", required=False)


class SpeakerResultForm(forms.Form):
    speaker = forms.ModelChoiceField(
        label="",
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
        required=False,
    )

    tie = forms.BooleanField(label="Tie", required=False)


class DebaterCreationFormsetBase(forms.BaseFormSet):
    required_fields = ['first_name', 'last_name', 'school']


class SchoolCreationFormsetBase(forms.BaseFormSet):
    required_fields = ['name']


    def clean(self):
        if not self.forms:
            return

        school_names = []
        for form in self.forms:
            # Skip forms that don't have cleaned_data or are already marked for deletion
            if not form.cleaned_data or form.cleaned_data.get('DELETE'):
                continue

            name = form.cleaned_data.get('name', '').strip()
            existing_school = form.cleaned_data.get('existing_school')

            # If linking to existing school, skip creating new one
            if existing_school:
                form.cleaned_data['DELETE'] = True
                continue

            if name:
                school_names.append(name)

        if school_names:
            existing_schools = set(School.objects.filter(name__in=school_names).values_list('name', flat=True))
            for form in self.forms:
                if not form.cleaned_data or form.cleaned_data.get('DELETE'):
                    continue

                name = form.cleaned_data.get('name', '').strip()
                if name in existing_schools:
                    form.cleaned_data['DELETE'] = True

IMPORT_FORMSET_PARAMS = {
    'extra': 0,
    'can_delete': True,
    'can_order': True,
    'max_num': 150,
}

CREATION_FORMSET_PARAMS = {
    'extra': 0,
    'can_delete': True,
    'can_order': False,
    'max_num': 500,
}

VarsityTeamResultFormset = formset_factory(TeamResultForm, **IMPORT_FORMSET_PARAMS)
NoviceTeamResultFormset = formset_factory(TeamResultForm, **IMPORT_FORMSET_PARAMS)
UnplacedTeamResultFormset = formset_factory(TeamResultForm, **IMPORT_FORMSET_PARAMS)

VarsitySpeakerResultFormset = formset_factory(SpeakerResultForm, **IMPORT_FORMSET_PARAMS)
NoviceSpeakerResultFormset = formset_factory(SpeakerResultForm, **IMPORT_FORMSET_PARAMS)

DebaterCreationFormset = formset_factory(DebaterForm, formset=DebaterCreationFormsetBase, **CREATION_FORMSET_PARAMS)
SchoolCreationFormset = formset_factory(SchoolForm, formset=SchoolCreationFormsetBase, **CREATION_FORMSET_PARAMS)


class TeamChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.long_name


class TOTYReaffForm(forms.ModelForm):
    old_team = TeamChoiceField(
        queryset=Team.objects.prefetch_related("debaters", "debaters__school").order_by(
            "debaters__school__name", "debaters__first_name", "debaters__last_name"
        ),
        label="Old Team",
    )
    new_team = TeamChoiceField(
        queryset=Team.objects.prefetch_related("debaters", "debaters__school").order_by(
            "debaters__school__name", "debaters__first_name", "debaters__last_name"
        ),
        label="New Team",
    )

    season = forms.ChoiceField(choices=settings.SEASONS, widget=forms.Select())

    class Meta:
        model = TOTYReaff
        fields = "__all__"


class QualPointsForm(forms.ModelForm):
    debater = forms.ModelChoiceField(
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
    )

    season = forms.ChoiceField(choices=settings.SEASONS, widget=forms.Select())

    class Meta:
        model = QualPoints
        fields = "__all__"


class ReaffForm(forms.ModelForm):
    old_debater = forms.ModelChoiceField(
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
        label="Old Debater",
    )

    new_debater = forms.ModelChoiceField(
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
        label="New Debater",
    )

    season = forms.ChoiceField(choices=settings.SEASONS, widget=forms.Select())

    class Meta:
        model = Reaff
        fields = "__all__"


class SOTYForm(forms.ModelForm):
    debater = forms.ModelChoiceField(
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
    )

    season = forms.ChoiceField(choices=settings.SEASONS, widget=forms.Select())

    class Meta:
        model = SOTY
        fields = "__all__"


class NOTYForm(forms.ModelForm):
    debater = forms.ModelChoiceField(
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
    )

    season = forms.ChoiceField(choices=settings.SEASONS, widget=forms.Select())

    class Meta:
        model = NOTY
        fields = "__all__"


class COTYForm(forms.ModelForm):
    school = forms.ModelChoiceField(
        queryset=School.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:school_autocomplete"),
    )

    season = forms.ChoiceField(choices=settings.SEASONS, widget=forms.Select())

    class Meta:
        model = COTY
        fields = "__all__"


class QUALForm(forms.ModelForm):
    debater = forms.ModelChoiceField(
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
    )

    season = forms.ChoiceField(choices=settings.SEASONS, widget=forms.Select())

    class Meta:
        model = QUAL
        fields = "__all__"


class MergeDebaterRequestForm(forms.Form):
    school_one = forms.ModelChoiceField(
        label="Debater A School",
        queryset=School.objects.none(),
        required=True,
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    debater_one = forms.ModelChoiceField(
        label="Debater A",
        queryset=Debater.objects.none(),
        required=True,
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    school_two = forms.ModelChoiceField(
        label="Debater B School",
        queryset=School.objects.none(),
        required=True,
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    debater_two = forms.ModelChoiceField(
        label="Debater B",
        queryset=Debater.objects.none(),
        required=True,
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    keep_debater = forms.ChoiceField(
        label="Keep Record For",
        choices=(
            ("debater_one", "Debater A"),
            ("debater_two", "Debater B"),
        ),
        widget=forms.RadioSelect(),
        required=True,
    )

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

        if self.user is None:
            raise ValueError("MergeDebaterRequestForm requires a user.")

        self.allowed_seasons = {
            str(settings.CURRENT_SEASON),
            str(int(settings.CURRENT_SEASON) - 1),
        }

        admin_schools = (
            School.objects.filter(admins__user=self.user)
            .distinct()
            .order_by("name")
        )
        active_schools = (
            School.objects.filter(
                debaters__latest_season__in=self.allowed_seasons
            )
            .distinct()
            .order_by("name")
        )

        self.fields["school_one"].queryset = admin_schools
        self.fields["school_two"].queryset = active_schools

        for field_name in ("school_one", "debater_one", "school_two", "debater_two"):
            widget = self.fields[field_name].widget
            widget.attrs.setdefault("class", "form-control")
            current_value = ""
            if self.is_bound:
                current_value = self.data.get(field_name, "")
            else:
                initial = self.initial.get(field_name)
                current_value = str(initial) if initial is not None else ""
            widget.attrs["data-selected"] = current_value

        self.fields["keep_debater"].widget.attrs.setdefault("class", "form-check-input")
        self.fields["keep_debater"].widget.option_inherits_attrs = True

        self._set_debater_queryset("debater_one", "school_one")
        self._set_debater_queryset("debater_two", "school_two")
        self._update_keep_choices()

    def _set_debater_queryset(self, debater_field, school_field):
        school_id = self.data.get(school_field) or self.initial.get(school_field)
        if not school_id:
            self.fields[debater_field].queryset = Debater.objects.none()
            return
        try:
            school_id = int(school_id)
        except (TypeError, ValueError):
            self.fields[debater_field].queryset = Debater.objects.none()
            return

        queryset = Debater.objects.filter(
            school_id=school_id,
            latest_season__in=self.allowed_seasons,
        ).order_by("last_name", "first_name")

        self.fields[debater_field].queryset = queryset

    def _update_keep_choices(self):
        debater_one = self._get_debater_from_data("debater_one")
        debater_two = self._get_debater_from_data("debater_two")

        choices = []
        if debater_one:
            choices.append(("debater_one", debater_one.name))
        else:
            choices.append(("debater_one", "Debater A"))

        if debater_two:
            choices.append(("debater_two", debater_two.name))
        else:
            choices.append(("debater_two", "Debater B"))

        self.fields["keep_debater"].choices = choices

    def _get_debater_from_data(self, field_name):
        debater_id = self.data.get(field_name) or self.initial.get(field_name)
        if not debater_id:
            return None
        try:
            return Debater.objects.select_related("school").get(pk=int(debater_id))
        except (ValueError, Debater.DoesNotExist):
            return None

    def _season_token(self, value):
        if value is None:
            return None
        return str(value).split("-")[0]

    def clean(self):
        cleaned_data = super().clean()

        debater_one = cleaned_data.get("debater_one")
        debater_two = cleaned_data.get("debater_two")
        keep = cleaned_data.get("keep_debater")

        if not debater_one or not debater_two or not keep:
            return cleaned_data

        if debater_one.pk == debater_two.pk:
            raise forms.ValidationError("Debaters must be different.")

        allowed = self.allowed_seasons

        season_one = self._season_token(debater_one.latest_season)
        season_two = self._season_token(debater_two.latest_season)

        if season_one not in allowed or season_two not in allowed:
            raise forms.ValidationError(
                "Both debaters must have competed in the current or previous season."
            )

        admin_school_ids = set(
            SchoolAdmin.objects.filter(user=self.user).values_list("school_id", flat=True)
        )

        if (
            debater_one.school_id not in admin_school_ids
            and debater_two.school_id not in admin_school_ids
        ):
            raise forms.ValidationError(
                "At least one selected debater must attend a school you administer."
            )

        keep_choices = {choice[0] for choice in self.fields["keep_debater"].choices}
        if keep not in keep_choices:
            raise forms.ValidationError("Please choose which record to keep.")

        if keep == "debater_one":
            primary, secondary = debater_one, debater_two
        else:
            primary, secondary = debater_two, debater_one

        existing_pending = MergeDebaterRequest.objects.filter(
            status=MergeDebaterRequest.STATUS_PENDING
        ).filter(
            Q(primary_debater=primary, secondary_debater=secondary)
            | Q(primary_debater=secondary, secondary_debater=primary)
        )

        if existing_pending.exists():
            raise forms.ValidationError(
                "A pending merge request already exists for these debaters."
            )

        cleaned_data["primary_debater"] = primary
        cleaned_data["secondary_debater"] = secondary

        return cleaned_data
