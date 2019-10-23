from dal import autocomplete

from django import forms

from core.models.debater import Debater
from core.models.school import School
from core.models.tournament import Tournament
from core.models.team import Team


class DebaterForm(forms.ModelForm):
    school = forms.ModelChoiceField(
        queryset=School.objects.all(),
        widget=autocomplete.ModelSelect2(url='core:school_autocomplete')
    )

    class Meta:
        model = Debater
        fields = ('first_name', 'last_name', 'school', 'status')


class TournamentForm(forms.ModelForm):
    host = forms.ModelChoiceField(
        queryset=School.objects.all(),
        widget=autocomplete.ModelSelect2(url='core:school_autocomplete')
    )

    class Meta:
        model = Tournament
        fields = ('host',
                  'num_rounds',            
                  'season',
                  'date',
                  'num_teams',
                  'num_novice_teams',
                  'num_debaters',
                  'num_novice_debaters',
                  'qual',
                  'noty',
                  'soty',
                  'toty',
                  'qual_bar')


class TeamForm(forms.ModelForm):
    debaters = forms.ModelMultipleChoiceField(
        queryset=Debater.objects.all(),
        widget=autocomplete.ModelSelect2Multiple(url='core:debater_autocomplete', forward=['school'])
    )

    school = forms.ModelChoiceField(
        queryset=School.objects.all(),
        widget=autocomplete.ModelSelect2(url='core:school_autocomplete')
    )

    class Meta:
        model = Team
        fields = ('school',
                  'debaters')

    def clean(self):
        cleaned_data = super().clean()

        for debater in cleaned_data.get('debaters'):
            if not debater.school == cleaned_data.get('school'):
                raise forms.ValidationError(
                    '%s is not from %s' % (debater.name, cleaned_data.get('school'))
                )

        if not len(cleaned_data.get('debaters')) == 2:
            raise forms.ValidationError(
                'All teams must have 2 debaters'
            )
