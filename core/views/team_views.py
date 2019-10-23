from django.urls import reverse_lazy

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
from core.models.team import Team
from core.forms import TeamForm


class TeamFilter(FilterSet):
    class Meta:
        model = Team
        fields = {
            'id': ['exact'],
            'name': ['icontains'],
         }


class TeamTable(CustomTable):
    id = Column(linkify=True)

    class Meta:
        model = Team
        fields = ('id',
                  'name',
                  'debaters')


class TeamListView(CustomListView):
    model = Team
    table_class = TeamTable
    template_name = 'teams/list.html'

    filterset_class = TeamFilter

    buttons = [
        {
            'name': 'Create',
            'href': reverse_lazy('core:team_create'),
            'perm': 'core.add_team',
            'class': 'btn-success'
        }
    ]


class TeamDetailView(CustomDetailView):
    model = Team
    template_name = 'teams/detail.html'

    buttons = [
        {
            'name': 'Delete',
            'href': 'core:team_delete',
            'perm': 'core.remove_team',
            'class': 'btn-danger',
            'include_pk': True
        },
        {
            'name': 'Edit',
            'href': 'core:team_update',
            'perm': 'core.change_team',
            'class': 'btn-info',
            'include_pk': True
        },
    ]


class TeamUpdateView(CustomUpdateView):
    model = Team

    form_class = TeamForm
    template_name = 'teams/update.html'

    def form_valid(self, form):
        to_return = super().form_valid(form)

        form.instance.update_name()
        form.instance.save()

        return to_return
    

class TeamCreateView(CustomCreateView):
    model = Team

    form_class = TeamForm
    template_name = 'teams/create.html'

    def form_valid(self, form):
        to_return = super().form_valid(form)

        form.instance.update_name()
        form.instance.save()

        return to_return


class TeamDeleteView(CustomDeleteView):
    model = Team
    success_url = reverse_lazy('core:team_list')

    template_name = 'teams/delete.html'


class TeamAutocomplete(autocomplete.Select2QuerySetView):
    def get_queryset(self):
        qs = Team.objects.all()

        if self.q:
            qs = qs.filter(name=self.q)

        return qs
