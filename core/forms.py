from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Div, Layout, Row, Submit
from dal import autocomplete
from django import forms
from django.conf import settings
from django.core.validators import URLValidator
from django.forms import formset_factory
from django_summernote.widgets import SummernoteInplaceWidget

from core.models import Team, TOTYReaff
from core.models.debater import Debater, QualPoints, Reaff
from core.models.results.speaker import SpeakerResult
from core.models.results.team import TeamResult
from core.models.school import School
from core.models.standings.coty import COTY
from core.models.standings.noty import NOTY
from core.models.standings.qual import QUAL
from core.models.standings.soty import SOTY
from core.models.tournament import Tournament
from core.models.video import Video


class DebaterForm(forms.ModelForm):
    school = forms.ModelChoiceField(
        queryset=School.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:school_autocomplete"),
    )

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


class TournamentSelectionForm(forms.Form):
    tournament = forms.ModelChoiceField(
        queryset=Tournament.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:tournament_autocomplete"),
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

    class Meta:
        model = TeamResult
        fields = []


class SpeakerResultForm(forms.ModelForm):
    speaker = forms.ModelChoiceField(
        label="",
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:debater_autocomplete"),
        required=False,
    )

    class Meta:
        model = SpeakerResult
        fields = ("speaker", "tie")


VarsityTeamResultFormset = formset_factory(TeamResultForm, extra=24, max_num=24)
NoviceTeamResultFormset = formset_factory(TeamResultForm, extra=8, max_num=8)
UnplacedTeamResultFormset = formset_factory(TeamResultForm, extra=20, max_num=20)

VarsitySpeakerResultFormset = formset_factory(SpeakerResultForm, extra=10, max_num=10)
NoviceSpeakerResultFormset = formset_factory(SpeakerResultForm, extra=10, max_num=10)


class SchoolReconciliationForm(forms.Form):
    id = forms.FloatField(widget=forms.HiddenInput())

    server_name = forms.CharField(label="Server School Name")

    school = forms.ModelChoiceField(
        queryset=School.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:school_autocomplete"),
        required=False,
    )


class DebaterReconciliationForm(forms.Form):
    id = forms.FloatField(widget=forms.HiddenInput())
    school_id = forms.FloatField(widget=forms.HiddenInput())
    status = forms.FloatField(widget=forms.HiddenInput())

    server_name = forms.CharField(label="Server Debater Name")
    server_school_name = forms.CharField(label="Server School Name", disabled=True)
    server_hybrid_school_name = forms.CharField(
        label="Server Hybrid School Name", disabled=True, required=False
    )

    school = forms.ModelChoiceField(
        queryset=School.objects.all(),
        widget=autocomplete.ModelSelect2(url="core:school_autocomplete"),
        required=False,
    )

    debater = forms.ModelChoiceField(
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2(
            url="core:debater_autocomplete", forward=["school"]
        ),
        required=False,
    )


SchoolReconciliationFormset = formset_factory(SchoolReconciliationForm, extra=0)
DebaterReconciliationFormset = formset_factory(DebaterReconciliationForm, extra=0)


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
