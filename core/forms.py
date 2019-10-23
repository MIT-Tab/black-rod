from dal import autocomplete

from django import forms

from core.models.debater import School, Debater


class DebaterForm(forms.ModelForm):
    school = forms.ModelChoiceField(
        queryset=School.objects.all(),
        widget=autocomplete.ModelSelect2(url='core:school_autocomplete', attrs={'theme': 'bootstrap'})
    )

    class Meta:
        model = Debater
        fields = ('first_name', 'last_name', 'school', 'status')
