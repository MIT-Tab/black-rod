from django.urls import reverse_lazy

from django_filters import FilterSet
from django_tables2 import Column

from core.utils.generics import (
    CustomListView,
    CustomTable,
    CustomCreateView,
    CustomUpdateView,
    CustomDetailView,
    CustomDeleteView
)
from core.models.tournament import Tournament
from core.forms import TournamentForm


class TournamentFilter(FilterSet):
    class Meta:
        model = Tournament
        fields = {
            'id': ['exact'],
            'name': ['icontains'],
            'season': ['exact'],
            'num_rounds': ['exact'],            
            'num_teams': ['gt'],
            'num_novice_teams': ['gt'],
            'num_debaters': ['gt'],
            'num_novice_debaters': ['gt'],
            'qual': ['exact'],
            'toty': ['exact'],
            'soty': ['exact'],
            'noty': ['exact'],
            'qual_bar': ['exact'],
        }


class TournamentTable(CustomTable):
    id = Column(linkify=True)

    class Meta:
        model = Tournament
        fields = ('id',
                  'name',
                  'host.name',
                  'season',
                  'num_rounds',
                  'num_teams',
                  'num_novice_debaters',
                  'qual',
                  'toty',
                  'soty',
                  'noty',
                  'qual_bar')


class TournamentListView(CustomListView):
    model = Tournament
    table_class = TournamentTable
    template_name = 'tournaments/list.html'

    filterset_class = TournamentFilter

    buttons = [
        {
            'name': 'Create',
            'href': reverse_lazy('core:tournament_create'),
            'perm': 'core.add_tournament',
            'class': 'btn-success'
        }
    ]


class TournamentDetailView(CustomDetailView):
    model = Tournament
    template_name = 'tournaments/detail.html'

    buttons = [
        {
            'name': 'Delete',
            'href': 'core:tournament_delete',
            'perm': 'core.remove_tournament',
            'class': 'btn-danger',
            'include_pk': True
        },
        {
            'name': 'Edit',
            'href': 'core:tournament_update',
            'perm': 'core.change_tournament',
            'class': 'btn-info',
            'include_pk': True
        },
    ]


class TournamentUpdateView(CustomUpdateView):
    model = Tournament

    form_class = TournamentForm
    template_name = 'tournaments/update.html'


class TournamentCreateView(CustomCreateView):
    model = Tournament

    form_class = TournamentForm
    template_name = 'tournaments/create.html'


class TournamentDeleteView(CustomDeleteView):
    model = Tournament
    success_url = reverse_lazy('core:tournament_list')

    template_name = 'tournaments/delete.html'
