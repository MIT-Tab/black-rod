from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Div, Layout, Row, Submit
from dal import autocomplete
from django import forms
from django.conf import settings
from django.core.validators import URLValidator
from django.db.models import Q
from django.forms import formset_factory
from django.forms.models import BaseModelFormSet
from django_summernote.widgets import SummernoteInplaceWidget

from core.models import Team, TOTYReaff
from core.models.debater import Debater, QualPoints, Reaff
from core.models.merge_request import MergeDebaterRequest
from core.models.claim_request import ClaimDebaterRequest
from core.models.debater_alias_group import DebaterAliasGroup
from core.models.school import School
from core.models.school_admin import SchoolAdmin
from core.models.standings.coty import COTY
from core.models.standings.noty import NOTY
from core.models.standings.qual import QUAL
from core.models.standings.soty import SOTY
from core.models.tournament import Tournament
from core.models.video import Video
from core.models.resource import Resource


class SchoolForm(forms.ModelForm):
    existing_school = forms.ModelChoiceField(
        queryset=School.objects.all(),
        required=False,
        label="Link to Existing School",
        help_text="If this school already exists under a different name, select it here instead of creating a new one",
        widget=autocomplete.ModelSelect2(url="core:school_autocomplete"),
    )
    included_in_oty = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.HiddenInput(),
    )
    server_name = forms.CharField(widget=forms.HiddenInput(), required=False)

    class Meta:
        model = School
        fields = ("name", "short_name", "included_in_oty")

    def clean(self):
        cleaned = super().clean()
        existing = cleaned.get("existing_school")
        name = cleaned.get("name") or ""
        short_name = cleaned.get("short_name")
        server_name = cleaned.get("server_name")

        # If name is missing (e.g., tab untouched), fall back to server_name to avoid bogus "required" errors.
        if not name and server_name:
            cleaned["name"] = server_name
            name = server_name

        # If the user is linking or creating without providing a short name, fall back gracefully.
        if not short_name:
            if existing and getattr(existing, "short_name", None):
                cleaned["short_name"] = existing.short_name
            else:
                cleaned["short_name"] = name

        return cleaned

    def __init__(self, *args, **kwargs):
        allow_blank_name = kwargs.pop("allow_blank_name", False)
        super().__init__(*args, **kwargs)
        # Allow short_name to be optional during API imports/linking
        self.fields["short_name"].required = False
        if allow_blank_name:
            self.fields["name"].required = False


class DebaterForm(forms.ModelForm):
    school = forms.ModelChoiceField(
        queryset=School.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:school_autocomplete"),
    )
    existing_debater = forms.ModelChoiceField(
        queryset=Debater.objects.all(),
        required=False,
        label="Link to Existing Debater",
        help_text="If this debater already exists under a different spelling, link them here.",
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
    )
    alias_group = forms.ModelChoiceField(
        queryset=DebaterAliasGroup.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:alias_group_autocomplete"),
        required=False,
        help_text="Optional: link this affiliation to an existing alias group.",
    )
    tournament_id = forms.CharField(widget=forms.HiddenInput(), required=False)
    school_name = forms.CharField(widget=forms.HiddenInput(), required=False)
    last_name = forms.CharField(max_length=32, required=False)

    class Meta:
        model = Debater
        fields = ("first_name", "last_name", "school", "alias_group")

    def __init__(self, *args, **kwargs):
        include_temporary_schools = kwargs.pop("include_temporary_schools", False)
        super().__init__(*args, **kwargs)
        self.fields["school"].queryset = (
            School.all_objects.all() if include_temporary_schools else School.objects.all()
        )
        self.fields["existing_debater"].queryset = Debater.objects.all()


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


class ResourceForm(forms.ModelForm):
    class Meta:
        model = Resource
        fields = (
            "title",
            "authors",
            "resource_type",
            "content_link",
            "description",
            "usage_permissions",
            "viewing_permission",
            "tags",
        )

        widgets = {
            "authors": autocomplete.ModelSelect2Multiple(url="core:debater_autocomplete"),
            "description": forms.Textarea(attrs={'rows': 5}),
            "usage_permissions": forms.Textarea(attrs={'rows': 4}),
            "tags": autocomplete.TaggitSelect2("core:resource_tag_autocomplete"),
        }
        
        labels = {
            "viewing_permission": "Viewing Permission",
            "content_link": "Content Link",
        }
        
        help_texts = {
            "viewing_permission": "Public means discoverable by Google search. Requires Login uses the same permission as viewing videos.",
            "authors": "At least one author is required. Only people with claimed debater profiles can be authors.",
            "tags": "",  # Remove default "A comma-separated list of tags." caption
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        # If user is not a superuser, restrict tag creation
        if self.user and not self.user.is_superuser:
            self.fields['tags'].widget = autocomplete.TaggitSelect2("core:resource_tag_autocomplete_no_create")
            
            # Set initial authors to user's claimed debaters if creating new resource
            if not self.instance.pk and self.user.claimed_debaters.exists():
                self.fields['authors'].initial = self.user.claimed_debaters.all()

        self.helper = FormHelper()
        self.helper.layout = Layout(
            Row(
                Column("title", css_class="col-md-8"),
                Column("resource_type", css_class="col-md-4"),
            ),
            Div(css_class="border-top my-3"),
            "authors",
            Div(css_class="border-top my-3"),
            "content_link",
            "description",
            "usage_permissions",
            Div(css_class="border-top my-3"),
            Row(
                Column("viewing_permission", css_class="col-md-6"),
                Column("tags", css_class="col-md-6"),
            ),
            Submit("save", "Save"),
        )
    
    def clean_authors(self):
        """Ensure at least one author is selected and non-superusers include themselves."""
        authors = self.cleaned_data.get('authors')
        if not authors or authors.count() == 0:
            raise forms.ValidationError("At least one author is required.")
        
        # If user is not a superuser, ensure they are an author
        if self.user and not self.user.is_superuser:
            user_debaters = self.user.claimed_debaters.all()
            if user_debaters.exists():
                # Check if at least one of the user's debaters is in the authors list
                if not any(debater in authors for debater in user_debaters):
                    raise forms.ValidationError(
                        "You must include yourself as an author. Please select one of your claimed debater profiles."
                    )
        
        return authors
        authors = self.cleaned_data.get('authors')
        if not authors or authors.count() == 0:
            raise forms.ValidationError("At least one author is required.")
        return authors


class TournamentForm(forms.ModelForm):
    host = forms.ModelChoiceField(
        queryset=School.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:school_autocomplete"),
    )

    season = forms.ChoiceField(choices=settings.SEASONS, widget=forms.Select())

    short_name = forms.CharField(
        required=False,
        max_length=128,
        help_text="Leave blank to auto-generate from host short name",
    )

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
            "short_name",
        )


class TournamentCreateForm(TournamentForm):
    pass


class TeamForm(forms.ModelForm):
    debaters = forms.ModelMultipleChoiceField(
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2Multiple(url="core:debater_autocomplete"),
    )

    class Meta:
        model = Team
        fields = ("debaters", "short_name")

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


class RoundAmendmentUploadForm(forms.Form):
    amendment_file = forms.FileField(
        label="Round amendment JSON",
        help_text="Upload a JSON file with synthetic resolutions, round edits, or tournament import moves/deletes.",
        widget=forms.ClearableFileInput(
            attrs={"accept": ".json,application/json"}
        ),
    )

    def clean_amendment_file(self):
        uploaded = self.cleaned_data["amendment_file"]
        name = str(getattr(uploaded, "name", "") or "").lower()
        if name and not name.endswith(".json"):
            raise forms.ValidationError("Please upload a .json amendment file.")
        return uploaded


class TournamentImportMoveForm(forms.Form):
    tournament_import_id = forms.IntegerField(widget=forms.HiddenInput())
    target_tournament = forms.ModelChoiceField(
        queryset=Tournament.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:all_tournament_autocomplete"),
    )

    def __init__(self, *args, **kwargs):
        current_tournament = kwargs.pop("current_tournament", None)
        super().__init__(*args, **kwargs)
        queryset = Tournament.objects.all().order_by("-date", "id")
        if current_tournament is not None:
            queryset = queryset.exclude(pk=getattr(current_tournament, "pk", current_tournament))
        self.fields["target_tournament"].queryset = queryset


class TournamentResultsImportOptionsForm(forms.Form):
    api_url = forms.URLField(
        required=False,
        label="Mit-Tab Tournament Import URL (Optional)",
        help_text="If provided, selected categories below are imported from this Mit-Tab tournament.",
        widget=forms.URLInput(attrs={"placeholder": "https://nu-tab.com/tournament/123"}),
    )
    import_varsity_teams = forms.BooleanField(required=False, initial=False)
    import_varsity_speakers = forms.BooleanField(required=False, initial=False)
    import_novice_teams = forms.BooleanField(required=False, initial=False)
    import_novice_speakers = forms.BooleanField(required=False, initial=False)
    import_unplaced_teams = forms.BooleanField(required=False, initial=False)
    import_counts = forms.BooleanField(
        required=False,
        initial=False,
        label="Import team and novice counts",
    )

    CATEGORY_FIELDS = {
        "varsity_teams": "import_varsity_teams",
        "varsity_speakers": "import_varsity_speakers",
        "novice_teams": "import_novice_teams",
        "novice_speakers": "import_novice_speakers",
        "unplaced_teams": "import_unplaced_teams",
    }

    def selected_result_tabs(self):
        if not hasattr(self, "cleaned_data"):
            return []
        return [
            tab_key
            for tab_key, field_name in self.CATEGORY_FIELDS.items()
            if self.cleaned_data.get(field_name)
        ]

    def clean(self):
        cleaned_data = super().clean()
        api_url = (cleaned_data.get("api_url") or "").strip()
        selected_categories = any(
            cleaned_data.get(field_name) for field_name in self.CATEGORY_FIELDS.values()
        ) or cleaned_data.get("import_counts")

        if selected_categories and not api_url:
            raise forms.ValidationError(
                "Provide a Mit-Tab tournament URL to import selected categories, or clear all import selections."
            )

        return cleaned_data

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
    counts_for_points = forms.BooleanField(
        label="Counts for points", required=False, initial=True
    )
    
    # Hidden fields for tracking new debaters by tournament ID
    debater_one_tournament_id = forms.CharField(widget=forms.HiddenInput(), required=False)
    debater_two_tournament_id = forms.CharField(widget=forms.HiddenInput(), required=False)

    def __init__(self, *args, **kwargs):
        include_temporary_debaters = kwargs.pop("include_temporary_debaters", False)
        super().__init__(*args, **kwargs)
        queryset = Debater.all_objects.all() if include_temporary_debaters else Debater.objects.all()
        self.fields["debater_one"].queryset = queryset
        self.fields["debater_two"].queryset = queryset


class SpeakerResultForm(forms.Form):
    speaker = forms.ModelChoiceField(
        label="",
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
        required=False,
    )

    tie = forms.BooleanField(label="Tie", required=False)
    counts_for_points = forms.BooleanField(
        label="Counts for points", required=False, initial=True
    )
    
    # Hidden field for tracking new speakers by tournament ID
    tournament_id = forms.CharField(widget=forms.HiddenInput(), required=False)

    def __init__(self, *args, **kwargs):
        include_temporary_debaters = kwargs.pop("include_temporary_debaters", False)
        super().__init__(*args, **kwargs)
        queryset = Debater.all_objects.all() if include_temporary_debaters else Debater.objects.all()
        self.fields["speaker"].queryset = queryset


class DebaterCreationFormsetBase(forms.BaseFormSet):
    required_fields = ['first_name', 'last_name', 'school']

    def clean(self):
        if not self.forms:
            return

        seen_signatures = set()
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get('DELETE'):
                continue

            existing_debater = form.cleaned_data.get('existing_debater')
            if existing_debater:
                form.cleaned_data['_existing_match'] = existing_debater
                form.cleaned_data['_skip_creation'] = True
                continue

            first_name = form.cleaned_data.get('first_name', '').strip()
            last_name = form.cleaned_data.get('last_name', '').strip()
            school = form.cleaned_data.get('school')

            if not (first_name and last_name and school):
                continue

            signature = (first_name.lower(), last_name.lower(), getattr(school, 'pk', None))
            if signature in seen_signatures:
                form.cleaned_data['_skip_creation'] = True
                continue
            seen_signatures.add(signature)

            existing_match = Debater.objects.filter(
                first_name__iexact=first_name,
                last_name__iexact=last_name,
                school=school,
            ).first()
            if existing_match:
                form.cleaned_data['_existing_match'] = existing_match
                form.cleaned_data['_skip_creation'] = True


class SchoolCreationFormsetBase(forms.BaseFormSet):
    required_fields = ['name']


    def clean(self):
        if not self.forms:
            return

        school_names = []
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get('DELETE'):
                continue
            name = form.cleaned_data.get('name', '').strip()
            if name:
                school_names.append(name)

        existing_schools_by_name = {
            s.name: s for s in School.objects.filter(name__in=school_names)
        } if school_names else {}

        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get('DELETE'):
                continue

            name = form.cleaned_data.get('name', '').strip()
            existing_school = form.cleaned_data.get('existing_school')

            # If linking to existing school, skip creating new one but keep data
            if existing_school:
                form.cleaned_data['_existing_match'] = existing_school
                form.cleaned_data['_skip_creation'] = True
            elif name and name in existing_schools_by_name:
                form.cleaned_data['_existing_match'] = existing_schools_by_name[name]
                form.cleaned_data['_skip_creation'] = True

            if form.cleaned_data.get('_skip_creation') and hasattr(form, '_errors'):
                # Allow linking or deduped rows to bypass unique-name validation
                form._errors.pop('name', None)

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


class DebaterImportFormsetBase(BaseModelFormSet):
    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        pk_name = self.model._meta.pk.name
        if pk_name in form.fields:
            form.fields[pk_name].required = False
            # Keep the primary key around even if it wasn't posted back
            if (
                form.instance
                and form.instance.pk
                and not form.data.get(f"{form.prefix}-{pk_name}", "").strip()
            ):
                form.initial[pk_name] = form.instance.pk
        return form

    def add_fields(self, form, index):
        super().add_fields(form, index)
        # Allow arbitrary id values without queryset validation; we resolve ids ourselves.
        form.fields["id"] = forms.IntegerField(
            required=False, widget=forms.HiddenInput(), initial=form.instance.pk
        )


class SchoolImportFormsetBase(BaseModelFormSet):
    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        pk_name = self.model._meta.pk.name
        if pk_name in form.fields:
            form.fields[pk_name].required = False
            if (
                form.instance
                and form.instance.pk
                and not form.data.get(f"{form.prefix}-{pk_name}", "").strip()
            ):
                form.initial[pk_name] = form.instance.pk
        return form

    def add_fields(self, form, index):
        super().add_fields(form, index)
        form.fields["id"] = forms.IntegerField(
            required=False, widget=forms.HiddenInput(), initial=form.instance.pk
        )


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

        # Superusers can select from any school
        if self.user.is_superuser:
            admin_schools = (
                School.objects.filter(
                    debaters__latest_season__in=self.allowed_seasons
                )
                .distinct()
                .order_by("name")
            )
            active_schools = admin_schools
        else:
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
        return str(value).split("-", maxsplit=1)[0]

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

        # Superusers can merge any debaters, regular users must administer at least one school
        if not self.user.is_superuser:
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


class ClaimDebaterRequestForm(forms.Form):
    school = forms.ModelChoiceField(
        label="Select School",
        queryset=School.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:school_autocomplete"),
        help_text="First, select the school you debated for",
    )

    debater = forms.ModelChoiceField(
        label="Select Debater to Claim",
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2(
            url="core:debater_autocomplete",
            forward=['school']
        ),
        help_text="Then search for and select your debater profile",
    )

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

        if self.user is None:
            raise ValueError("ClaimDebaterRequestForm requires a user.")

    def clean(self):
        cleaned_data = super().clean()
        school = cleaned_data.get('school')
        debater = cleaned_data.get('debater')

        # Verify the debater belongs to the selected school
        if school and debater and debater.school != school:
            raise forms.ValidationError(
                "The selected debater does not belong to the selected school."
            )

        return cleaned_data

    def clean_debater(self):
        debater = self.cleaned_data.get("debater")

        if not debater:
            return debater

        # Check if debater already has a user
        if debater.user:
            raise forms.ValidationError(
                "This debater has already been claimed by another user."
            )

        # Check if there's already a pending request for this debater
        existing_pending = ClaimDebaterRequest.objects.filter(
            debater=debater,
            status=ClaimDebaterRequest.STATUS_PENDING
        ).exists()

        if existing_pending:
            raise forms.ValidationError(
                "A pending claim request already exists for this debater."
            )

        # Check if the user already has a pending request for this debater
        user_pending = ClaimDebaterRequest.objects.filter(
            requested_by=self.user,
            debater=debater,
            status=ClaimDebaterRequest.STATUS_PENDING
        ).exists()

        if user_pending:
            raise forms.ValidationError(
                "You already have a pending claim request for this debater."
            )

        return debater


class DebaterProfileEditForm(forms.ModelForm):
    SEASON_LOWEST_YEAR = 2004

    dino_to_contact_opt_in = forms.BooleanField(
        required=False,
        label="I'm open to TO outreach",
        help_text="Check this if you'd like tournaments to reach out when they need TOs.",
    )
    dino_judge_contact_opt_in = forms.BooleanField(
        required=False,
        label="I'm open to judging outreach",
        help_text="Check this if you'd like tournaments to reach out when they need judges. (Dinos only)",
    )
    region = forms.MultipleChoiceField(
        required=False,
        label="Where are you located?",
        choices=list(Debater.REGION_CHOICES),
        help_text="Let tournaments know your general region for outreach planning.",
        widget=forms.SelectMultiple(attrs={'class': 'form-control select2-multi'}),
    )
    paradigm = forms.URLField(
        required=False,
        label="Paradigm (Google Doc Link)",
        help_text="Link to your Google Doc paradigm. Make sure sharing is enabled so others can view it.",
        widget=forms.URLInput(attrs={'placeholder': 'https://docs.google.com/document/d/...'})
    )
    elo_manual_opt = forms.ChoiceField(
        required=False,
        label="ELO Inclusion Override",
        choices=Debater.EloManualOpt.choices,
        help_text="Override the default ELO inclusion heuristic for this profile.",
    )

    class Meta:
        model = Debater
        fields = [
            'first_name',
            'last_name',
            'status',
            'first_season',
            'latest_season',
            'elo_manual_opt',
            'paradigm',
            'dino_to_contact_opt_in',
            'dino_judge_contact_opt_in',
            'region',
        ]
        labels = {
            'first_name': 'First Name',
            'last_name': 'Last Name',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.show_to_contact_opt_in = True  # Always show TO opt-in
        self.show_judge_contact_opt_in = self._should_show_judge_contact_field()
        self.show_region_field = self._should_show_region_field()

        # Status select setup
        self.fields['status'].label = "Status"
        self.fields['status'].choices = Debater.STATUS
        self.fields['status'].widget.attrs.setdefault('class', 'form-control')
        self.fields['status'].widget.attrs['data-dino-value'] = str(Debater.DINO)

        # Season dropdowns
        current_year = int(settings.CURRENT_SEASON)
        first_initial = self.instance.first_season or ""
        latest_initial = self.instance.latest_season or ""

        self.fields['first_season'] = forms.ChoiceField(
            required=False,
            label="First Season",
            choices=self._season_choices(current_year, self.SEASON_LOWEST_YEAR, first_initial),
            initial=first_initial,
            widget=forms.Select(attrs={'class': 'form-control'}),
        )

        self.fields['latest_season'] = forms.ChoiceField(
            required=False,
            label="Latest Season",
            choices=self._season_choices(current_year, self.SEASON_LOWEST_YEAR, latest_initial),
            initial=latest_initial,
            widget=forms.Select(attrs={'class': 'form-control'}),
        )

        # Text inputs should use consistent styling
        for field_name in ('first_name', 'last_name', 'paradigm', 'elo_manual_opt'):
            if field_name in self.fields:
                self.fields[field_name].widget.attrs.setdefault('class', 'form-control')

        # TO opt-in always gets form-check-input
        self.fields['dino_to_contact_opt_in'].widget.attrs['class'] = 'form-check-input'

        # Judge opt-in gets form-check-input and dino-only data attribute
        judge_field = self.fields['dino_judge_contact_opt_in']
        judge_field.widget.attrs['class'] = 'form-check-input'
        judge_field.widget.attrs['data-dino-only'] = 'true'

        region_field = self.fields['region']
        region_field.widget.attrs.setdefault('class', 'form-control')
        region_field.widget.attrs['class'] += ' select2-multi'
        region_field.widget.attrs.setdefault('data-placeholder', 'Select regions')
        region_field.choices = list(Debater.REGION_CHOICES)
        region_field.initial = self.instance.region_list

    def _season_choices(self, start_year, min_year, include_value=None):
        def format_label(year_value):
            try:
                year_int = int(year_value)
            except (TypeError, ValueError):
                return str(year_value)
            next_year = str(year_int + 1)[-2:]
            return f"{year_int}-{next_year}"

        choices = [('', 'Select season')]
        for year in range(start_year, min_year - 1, -1):
            year_str = str(year)
            choices.append((year_str, format_label(year_str)))

        if include_value and include_value not in {choice[0] for choice in choices if choice[0]}:
            choices.append((include_value, format_label(include_value)))

        return choices

    def _should_show_judge_contact_field(self):
        """Judge contact field is only for dinos"""
        status_source = None
        if self.is_bound:
            status_source = self.data.get(self.add_prefix('status'))
        elif 'status' in self.initial:
            status_source = self.initial['status']
        elif hasattr(self.instance, 'status'):
            status_source = self.instance.status

        try:
            return int(status_source) == Debater.DINO
        except (TypeError, ValueError):
            return status_source == Debater.DINO

    def _get_boolean_value(self, field_name):
        if self.is_bound:
            value = self.data.get(self.add_prefix(field_name))
        elif field_name in self.initial:
            value = self.initial[field_name]
        elif hasattr(self.instance, field_name):
            value = getattr(self.instance, field_name)
        else:
            value = False

        if isinstance(value, str):
            return value.lower() in {'true', '1', 'on', 'yes'}
        return bool(value)

    def _should_show_region_field(self):
        return self._get_boolean_value('dino_to_contact_opt_in') or self._get_boolean_value('dino_judge_contact_opt_in')

    def clean_paradigm(self):
        paradigm = self.cleaned_data.get('paradigm')

        if paradigm and 'docs.google.com' not in paradigm:
            raise forms.ValidationError(
                "Paradigm must be a Google Docs link. Please ensure it's a link to a Google Doc."
            )

        return paradigm

    def clean(self):
        cleaned_data = super().clean()
        first_season = cleaned_data.get('first_season') or ''
        latest_season = cleaned_data.get('latest_season') or ''

        if first_season and latest_season:
            try:
                if int(first_season) > int(latest_season):
                    raise forms.ValidationError("First season cannot be after latest season.")
            except ValueError:
                raise forms.ValidationError("Season values must be valid years.")

        status = cleaned_data.get('status')
        try:
            status_value = int(status)
        except (TypeError, ValueError):
            status_value = None

        # Only clear judge opt-in for non-dinos; TO opt-in is available for all
        if status_value != Debater.DINO:
            cleaned_data['dino_judge_contact_opt_in'] = False

        cleaned_data['status'] = status_value

        cleaned_data['region'] = cleaned_data.get('region') or []

        to_outreach = cleaned_data.get('dino_to_contact_opt_in')
        judge_outreach = cleaned_data.get('dino_judge_contact_opt_in')
        if not (to_outreach or judge_outreach):
            cleaned_data['region'] = []

        return cleaned_data
