from django.urls import reverse_lazy
from django.db.models import Q

from django_filters import FilterSet

from dal import autocomplete

from django_tables2 import Column

from core.utils.generics import (
    CustomListView,
    CustomTable,
    CustomCreateView,
    CustomUpdateView,
    CustomDetailView,
    CustomDeleteView
)
from core.models.debater import Debater
from core.forms import DebaterForm


class DebaterFilter(FilterSet):
    class Meta:
        model = Debater
        fields = {
            'id': ['exact'],
            'first_name': ['icontains'],
            'last_name': ['icontains'],
            'school__name': ['icontains'],
            'status': ['exact']
        }


class DebaterTable(CustomTable):
    id = Column(linkify=True)

    class Meta:
        model = Debater
        fields = ('id',
                  'first_name',
                  'last_name',
                  'school.name',
                  'status')


class DebaterListView(CustomListView):
    model = Debater
    table_class = DebaterTable
    template_name = 'debaters/list.html'

    filterset_class = DebaterFilter

    buttons = [
        {
            'name': 'Create',
            'href': reverse_lazy('core:debater_create'),
            'perm': 'core.add_debater',
            'class': 'btn-success'
        }
    ]


class DebaterDetailView(CustomDetailView):
    model = Debater
    template_name = 'debaters/detail.html'

    buttons = [
        {
            'name': 'Delete',
            'href': 'core:debater_delete',
            'perm': 'core.remove_debater',
            'class': 'btn-danger',
            'include_pk': True
        },
        {
            'name': 'Edit',
            'href': 'core:debater_update',
            'perm': 'core.change_debater',
            'class': 'btn-info',
            'include_pk': True
        },
    ]


class DebaterUpdateView(CustomUpdateView):
    model = Debater

    form_class = DebaterForm
    template_name = 'debaters/update.html'


class DebaterCreateView(CustomCreateView):
    model = Debater

    form_class = DebaterForm
    template_name = 'debaters/create.html'


class DebaterDeleteView(CustomDeleteView):
    model = Debater
    success_url = reverse_lazy('core:debater_list')

    template_name = 'debaters/delete.html'


class DebaterAutocomplete(autocomplete.Select2QuerySetView):
    def get_queryset(self):
        qs = Debater.objects.all()

        if self.q:
            query = Q(first_name=self.q) | Q(last_name=self.q)
            qs = qs.filter(query)

        school = self.forwarded.get('school', None)

        if school:
            qs = qs.filter(school__id=school)

        return qs
    
