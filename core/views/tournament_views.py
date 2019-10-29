from django.urls import reverse_lazy
from django.shortcuts import redirect
from django.conf import settings

from django_filters import FilterSet

from dal import autocomplete

from django_tables2 import Column

from formtools.wizard.views import SessionWizardView

from core.utils.generics import (
    CustomMixin,
    CustomListView,
    CustomTable,
    CustomCreateView,
    CustomUpdateView,
    CustomDetailView,
    CustomDeleteView
)
from core.models.tournament import Tournament
from core.models.debater import Debater

from core.models.results.team import TeamResult
from core.models.results.speaker import SpeakerResult

from core.models.standings.toty import TOTY
from core.models.standings.soty import SOTY
from core.models.standings.noty import NOTY
from core.models.standings.coty import COTY
from core.models.standings.qual import QUAL

from core.utils.rankings import (
    redo_rankings,
    update_toty,
    update_soty,
    update_noty,
    update_qual_points,
)
from core.utils.team import get_or_create_team_for_debaters

from core.forms import (
    DebaterForm,
    TournamentForm,
    TournamentSelectionForm,
    
    VarsityTeamResultFormset,
    NoviceTeamResultFormset,
    VarsitySpeakerResultFormset,
    NoviceSpeakerResultFormset
)


class TournamentFilter(FilterSet):
    class Meta:
        model = Tournament
        fields = {
            'name': ['icontains'],
            'season': ['exact'],
            'qual_type': ['exact'],
        }


class TournamentTable(CustomTable):
    name = Column(linkify=True)

    class Meta:
        model = Tournament
        fields = ('name',
                  'date',
                  'season',
                  'num_teams',
                  'num_novice_debaters')



class TournamentListView(CustomListView):
    public_view = True    
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
        },
        {
            'name': 'Enter Results',
            'href': reverse_lazy('core:tournament_dataentry'),
            'perm': 'core.change_tournament',
            'class': 'btn-primary'
        }
    ]


class TournamentDetailView(CustomDetailView):
    public_view = True    
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

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        obj = self.object
            
        context['varsity_team_results'] = obj.team_results.filter(
            type_of_place=Debater.VARSITY
        ).order_by(
            'place'
        )

        context['novice_team_results'] = obj.team_results.filter(
            type_of_place=Debater.NOVICE
        ).order_by(
            'place'
        )

        context['varsity_speaker_results'] = obj.speaker_results.filter(
            type_of_place=Debater.VARSITY
        ).order_by(
                'place'
        )

        context['novice_speaker_results'] = obj.speaker_results.filter(
            type_of_place=Debater.NOVICE,
        ).order_by(
            'place'
        )

        return context


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


class TournamentAutocomplete(autocomplete.Select2QuerySetView):
    def get_result_label(self, record):
        return '<%s> %s (%s)' % (record.id,
                                 record.name,
                                 record.get_season_display())
    
    def get_queryset(self):
        qs = Tournament.objects.all()

        if self.q:
            qs = qs.filter(name__icontains=self.q)

        return qs


class TournamentDataEntryWizardView(CustomMixin, SessionWizardView):
    permission_required = 'core.change_tournament'

    step_names = {
        '0': 'Tournament Selection',
        '1': 'Varsity Team Awards',
        '2': 'Varsity Speaker Awards',
        '3': 'Novice Team Awards',
        '4': 'Novice Speaker Awards',
    }

    form_list = [
        TournamentSelectionForm,
        VarsityTeamResultFormset,
        VarsitySpeakerResultFormset,
        NoviceTeamResultFormset,
        NoviceSpeakerResultFormset
    ]
    template_name = 'tournaments/data_entry.html'

    def get_template_names(self):
        if self.steps.current == '0':
            return ['tournaments/tournament_entry.html']
        if self.steps.current == '1':
            return ['tournaments/team_result_entry.html']
        if self.steps.current == '2':
            return ['tournaments/speaker_result_entry.html']            
        if self.steps.current == '3':
            return ['tournaments/team_result_entry.html']            
        if self.steps.current == '4':
            return ['tournaments/speaker_result_entry.html']            

        return super().get_template_names()

    def get_form_initial(self, step):
        storage_data = None
        tournament = None

        form_kwargs = super().get_form_initial(step)

        initial = []
        
        if not step == '0':
            storage_data = self.storage.get_step_data('0')

            tournament = Tournament.objects.get(id=int(storage_data.get('0-tournament')))

        if step == '0' and 'tournament' in self.request.GET:
            tournament = Tournament.objects.filter(id=int(self.request.GET.get('tournament'))).first()

            if tournament:
                initial = {'tournament': tournament}

        if step == '1':
            results = TeamResult.objects.filter(tournament=tournament,
                                                type_of_place=Debater.VARSITY)

            for i in range(1, 17):
                if results.filter(place=i).exists():
                    initial += [{'debater_one': results.filter(place=i).first().team.debaters.first(),
                                 'debater_two': results.filter(place=i).first().team.debaters.last()}]
            
        if step == '2':
            results = SpeakerResult.objects.filter(tournament=tournament,
                                                type_of_place=Debater.VARSITY)

            for i in range(1, 11):
                if results.filter(place=i).exists():
                    initial += [{'speaker': results.filter(place=i).first().debater}]

        if step == '3':
            results = TeamResult.objects.filter(tournament=tournament,
                                                type_of_place=Debater.NOVICE)

            for i in range(1, 9):
                if results.filter(place=i).exists():
                    initial += [{'debater_one': results.filter(place=i).first().team.debaters.first(),
                                 'debater_two': results.filter(place=i).first().team.debaters.last()}]

        if step == '4':
            results = SpeakerResult.objects.filter(tournament=tournament,
                                                   type_of_place=Debater.NOVICE)

            for i in range(1, 17):
                if results.filter(place=i).exists():
                    initial += [{'speaker': results.filter(place=i).first().debater}]

        return initial

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        context['title'] = self.step_names[self.steps.current]
        context['debater_form'] = DebaterForm()

        return context

    def done(self, form_list, form_dict, **kwargs):
        tournament = form_dict['0'].cleaned_data['tournament']

        update_otys = settings.CURRENT_SEASON == tournament.season

        teams_to_update = []
        speakers_to_update = []
        novices_to_update = []

        ## THIS DOESN'T DO INITIAL

        for result in TeamResult.objects.filter(tournament=tournament).all():
            teams_to_update += [result.team]

            result.delete()

        for result in SpeakerResult.objects.filter(tournament=tournament).all():
            if result.type_of_place == Debater.VARSITY:
                speakers_to_update += [result.debater]
            else:
                novices_to_update += [result.debater]
            result.delete()

        QUAL.objects.filter(tournament=tournament).delete()
        
        ## VARSITY TEAM AWARDS ##
        for i in range(len(form_dict['1'].cleaned_data)):
            if not 'debater_one' in form_dict['1'].cleaned_data[i] or \
               not 'debater_two' in form_dict['1'].cleaned_data[i]:
                continue

            if not form_dict['1'].cleaned_data[i]['debater_one'] or \
               not form_dict['1'].cleaned_data[i]['debater_two']:
                continue

            team = get_or_create_team_for_debaters(
                form_dict['1'].cleaned_data[i]['debater_one'],
                form_dict['1'].cleaned_data[i]['debater_two']
            )

            if not team:
                continue

            place = i + 1
            type_of_place = Debater.VARSITY

            TeamResult.objects.create(tournament=tournament,
                                      team=team,
                                      type_of_place=type_of_place,
                                      place=place)

            teams_to_update += [team]

        for i in range(len(form_dict['2'].cleaned_data)):
            if not 'speaker' in form_dict['2'].cleaned_data[i]:
                continue

            place = i + 1
            type_of_place = Debater.VARSITY
            speaker = form_dict['2'].cleaned_data[i]['speaker']

            if not speaker:
                continue

            SpeakerResult.objects.create(tournament=tournament,
                                         debater=speaker,
                                         type_of_place=type_of_place,
                                         place=place)

            speakers_to_update += [speaker]


        for i in range(len(form_dict['3'].cleaned_data)):
            if not 'debater_one' in form_dict['3'].cleaned_data[i] or \
               not 'debater_two' in form_dict['3'].cleaned_data[i]:
                continue

            if not form_dict['3'].cleaned_data[i]['debater_one'] or \
               not form_dict['3'].cleaned_data[i]['debater_two']:
                continue

            team = get_or_create_team_for_debaters(
                form_dict['3'].cleaned_data[i]['debater_one'],
                form_dict['3'].cleaned_data[i]['debater_two']
            )

            if not team:
                continue

            place = i + 1
            type_of_place = Debater.NOVICE

            TeamResult.objects.create(tournament=tournament,
                                      team=team,
                                      type_of_place=type_of_place,
                                      place=place)

            teams_to_update += [team]

        for i in range(len(form_dict['4'].cleaned_data)):
            if not 'speaker' in form_dict['4'].cleaned_data[i]:
                continue

            place = i + 1
            type_of_place = Debater.NOVICE
            speaker = form_dict['4'].cleaned_data[i]['speaker']

            if not speaker:
                continue

            SpeakerResult.objects.create(tournament=tournament,
                                         debater=speaker,
                                         type_of_place=type_of_place,
                                         place=place)

            novices_to_update += [speaker]

        teams_to_update = list(set(teams_to_update))
        speakers_to_update = list(set(speakers_to_update))
        novices_to_update = list(set(novices_to_update))

        print ('TEAMS TO UPDATE: %s' % (teams_to_update,))

        if update_otys:
            for team in teams_to_update:
                update_toty(team)
                update_qual_points(team)
            for debater in speakers_to_update:
                update_soty(debater)
            for debater in novices_to_update:
                update_noty(debater)

            redo_rankings(TOTY.objects.filter(season=settings.CURRENT_SEASON))
            redo_rankings(SOTY.objects.filter(season=settings.CURRENT_SEASON))
            redo_rankings(NOTY.objects.filter(season=settings.CURRENT_SEASON))
            redo_rankings(COTY.objects.filter(season=settings.CURRENT_SEASON))
            
        return redirect('core:tournament_detail', pk=tournament.id)
