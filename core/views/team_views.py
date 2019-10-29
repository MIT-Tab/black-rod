from django.urls import reverse_lazy

from django_filters import FilterSet

from django.db.models import Q

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
            'name': ['icontains'],
         }


class TeamTable(CustomTable):
    name = Column(linkify=True)

    class Meta:
        model = Team
        fields = ('name',
                  'debaters')


class TeamListView(CustomListView):
    public_view = True
    model = Team
    table_class = TeamTable
    template_name = 'teams/list.html'

    filterset_class = TeamFilter


class TeamDetailView(CustomDetailView):
    public_view = True    
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

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        context['team_results'] = self.object.team_results.order_by(
            'tournament__date'
        )

        context['totys'] = self.object.toty.order_by(
            '-points',
            'season'
        )

        return context


class TeamUpdateView(CustomUpdateView):
    model = Team

    form_class = TeamForm
    template_name = 'teams/update.html'

    def form_valid(self, form):
        to_return = super().form_valid(form)

        form.instance.update_name()
        form.instance.save()

        return to_return

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        context['team_results'] = self.object.team_results.order_by(
            '-tournament__date'
        )

        context['totys'] = self.object.toty.order_by(
            '-season'
        )

        return context    
    

class TeamDeleteView(CustomDeleteView):
    model = Team
    success_url = reverse_lazy('core:team_list')

    template_name = 'teams/delete.html'


class TeamAutocomplete(autocomplete.Select2QuerySetView):
    def get_result_label(self, record):
        return '<%s> %s (%s)' % (record.id,
                                 record.name,
                                 ', '.join([d.name for d in record.debaters.all()]))
    
    def get_queryset(self):
        qs = Team.objects.all()

        if self.q:
            query = Q()
            for term in self.q.split():
                query = query | \
                        Q(name__icontains=term) | \
                        Q(debaters__first_name__icontains=term) | \
                        Q(debaters__last_name__icontains=term)
            qs = qs.filter(query).distinct()

        return qs
