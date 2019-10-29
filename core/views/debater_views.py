from django.urls import reverse_lazy
from django.db.models import Q

from django.conf import settings

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
from core.models.results.team import TeamResult
from core.models.standings.toty import TOTY

from core.forms import DebaterForm


class DebaterFilter(FilterSet):
    class Meta:
        model = Debater
        fields = {
            'first_name': ['icontains'],
            'last_name': ['icontains'],
            'school__name': ['icontains'],
            'status': ['exact']
        }


class DebaterTable(CustomTable):
    first_name = Column(linkify=True)
    last_name = Column(linkify=True)

    class Meta:
        model = Debater
        fields = ('first_name',
                  'last_name',
                  'school.name',
                  'status')


class DebaterListView(CustomListView):
    public_view = True
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
    public_view = True
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

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        tournaments = [result.tournament \
                       for result in TeamResult.objects.filter(
                               team__debaters=self.object
                       ).all()]
        tournaments += [result.tournament \
                        for result in self.object.speaker_results.all()]

        tournaments = list(set(tournaments))

        seasons = [tournament.season for tournament in tournaments]
        seasons = list(set(seasons))

        seasons.sort(key=lambda season: season, reverse=True)
        current_season = settings.CURRENT_SEASON

        if not len(seasons) == 0:
            current_season = self.request.GET.get('season', seasons[0])

        seasons = [season \
                   for season in settings.SEASONS if season[0] in seasons]

        seasons.sort(key=lambda season: season[0], reverse=True)

        context['seasons'] = seasons

        context['current_season'] = current_season

        tournaments = [tournament \
                       for tournament in tournaments if tournament.season == current_season]

        tournaments.sort(key=lambda tournament: tournament.date)

        tournament_render = []

        for tournament in tournaments:
            to_add = {}
            to_add['tournament'] = tournament
            to_append = []

            to_append += [('team', result) \
                          for result in TeamResult.objects.filter(
                                  team__debaters=self.object
                          ).filter(
                              tournament=tournament
                          ).order_by('-type_of_place').all()]
            to_append += [('speaker', result) \
                          for result in self.object.speaker_results.filter(
                                  tournament=tournament
                          ).order_by('-type_of_place').all()]

            team_result = TeamResult.objects.filter(
                team__debaters=self.object
            ).filter(
                tournament=tournament
            ).first()

            team = None if not team_result else team_result.team

            to_add['team'] = team
            to_add['data'] = to_append

            tournament_render.append(to_add)

        context['results'] = tournament_render        

        context['totys'] = TOTY.objects.filter(
            team__debaters=self.object
        ).order_by(
            'place',
            'season'
        )

        context['sotys'] = self.object.soty.order_by(
            'place',
            'season'
        )

        context['notys'] = self.object.noty.order_by(
            'place',
            'season'
        )

        teams = [(team, team.toty_points) for team in self.object.teams.all()]
        teams.sort(key=lambda team: team[1], reverse=True)

        context['teams'] = teams

        return context


class DebaterUpdateView(CustomUpdateView):
    model = Debater

    form_class = DebaterForm
    template_name = 'debaters/update.html'

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        context['totys'] = TOTY.objects.filter(
            team__debaters=self.object
        ).order_by(
            'place',
            'season'
        )

        context['sotys'] = self.object.soty.order_by(
            'place',
            'season'
        )

        context['notys'] = self.object.noty.order_by(
            'place',
            'season'
        )

        teams = [(team, team.toty_points) for team in self.object.teams.all()]
        teams.sort(key=lambda team: team[1], reverse=True)

        context['teams'] = teams

        return context    


class DebaterCreateView(CustomCreateView):
    model = Debater

    form_class = DebaterForm
    template_name = 'debaters/create.html'


class DebaterDeleteView(CustomDeleteView):
    model = Debater
    success_url = reverse_lazy('core:debater_list')

    template_name = 'debaters/delete.html'


class DebaterAutocomplete(autocomplete.Select2QuerySetView):
    def get_result_label(self, record):
        return '<%s> %s (%s)' % (record.id,
                                 record.name,
                                 record.school.name)
    
    def get_queryset(self):
        qs = Debater.objects.all()

        if self.q:
            query = Q()
            for query in self.q.split():
                query = Q(first_name__icontains=query) | \
                        Q(last_name__icontains=query)

            qs = qs.filter(query).distinct()

        school = self.forwarded.get('school', None)

        if school:
            qs = qs.filter(school__id=school)

        return qs
    
