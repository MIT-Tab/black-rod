import requests

from datetime import timedelta

from django.urls import reverse_lazy
from django.shortcuts import redirect
from django.conf import settings
from django.db.models import Q

from django.views.generic import TemplateView

from django.http import QueryDict

from django_filters import FilterSet

from haystack.query import SearchQuerySet

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
from core.models.school import School
from core.models.team import Team
from core.models.round import Round

from core.models.results.team import TeamResult
from core.models.results.speaker import SpeakerResult

from core.models.standings.toty import TOTY
from core.models.standings.soty import SOTY
from core.models.standings.noty import NOTY
from core.models.standings.coty import COTY
from core.models.standings.qual import QUAL
from core.models.standings.online_qual import OnlineQUAL

from core.utils.rankings import (
    redo_rankings,
    update_toty,
    update_soty,
    update_noty,
    update_qual_points,
    update_online_quals
)
from core.utils.import_management import (
    CREATE, LINK,
    get_dict,
    get_num_teams,
    get_num_novice_debaters,
    clean_keys,
    create_schools,
    create_debaters,
    create_teams,
    create_rounds,
    create_round_stats,
    create_speaker_awards,
    create_team_awards,
    lookup_school
)
from core.utils.team import get_or_create_team_for_debaters
from core.utils.rounds import get_tab_card_data

from core.forms import (
    DebaterForm,
    TournamentForm,
    TournamentSelectionForm,
    TournamentDetailForm,
    VarsityTeamResultFormset,
    NoviceTeamResultFormset,
    VarsitySpeakerResultFormset,
    NoviceSpeakerResultFormset,
    TournamentImportForm,
    SchoolReconciliationFormset,
    DebaterReconciliationFormset
)


class TournamentFilter(FilterSet):
    def __init__(self, data=None, *args, **kwargs):
        if not data:
            data = QueryDict('season=%s' % (settings.CURRENT_SEASON,))

        super().__init__(data, *args, **kwargs)

    class Meta:
        model = Tournament
        fields = {
            'id': ['exact'],
            'name': ['icontains'],
            'season': ['exact'],
            'qual_type': ['exact'],
        }


class TournamentTable(CustomTable):
    id = Column(linkify=True)

    name = Column(linkify=True)

    class Meta:
        model = Tournament
        fields = ('id',
                  'name',
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
        },
        {
            'name': 'Import Results',
            'href': reverse_lazy('core:tournament_import'),
            'perm': 'core.change_tournament',
            'class': 'btn-info'
        }
    ]

    def get_queryset(self, *args, **kwargs):
        qs = super().get_queryset(*args, **kwargs)

        ids = []

        for q in qs:
            if q.team_results.count() > 0 or q.speaker_results.count() > 0:
                ids += [q.id]

        qs = qs.filter(id__in=ids)

        return qs


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

        vspeakers = list(obj.speaker_results.filter(
            type_of_place=Debater.VARSITY
        ).order_by('place')) 

        vspeakerCount = len(vspeakers)
        for i in range(vspeakerCount):
            if vspeakers[i].tie:
                vspeakers[i].place -= 1  
            if i < vspeakerCount - 1 and vspeakers[i + 1].tie:
                vspeakers[i].tie = True  


        context['varsity_speaker_results'] = vspeakers


        nspeakers = list(obj.speaker_results.filter(
            type_of_place=Debater.NOVICE,
        ).order_by('place')) 

        nspeakerCount = len(nspeakers)
        for i in range(nspeakerCount):
            if i < nspeakerCount - 1 and nspeakers[i + 1].tie:
                nspeakers[i].tie = True 
            if nspeakers[i].tie:
                nspeakers[i].place -= 1  

        context['novice_speaker_results'] = nspeakers
       

        context['novice_speaker_results'] = nspeakers

        context['tab_cards_available'] = Round.objects.filter(tournament=self.object).exists()

        teams = Team.objects.filter(
            Q(govs__tournament=self.object) | Q(opps__tournament=self.object)
        ).distinct().all()

        context['teams'] = [(team, get_tab_card_data(team, self.object)) for team in teams]
                            
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


class AllTournamentAutocomplete(autocomplete.Select2QuerySetView):
    def get_result_label(self, record):
        return '<%s> %s (%s)' % (record.id,
                                 record.name,
                                 record.get_season_display())
    
    def get_queryset(self):
        qs = Tournament.objects.all()

        if self.q:
            qs = qs.filter(name__icontains=self.q)

        return qs


class TournamentAutocomplete(autocomplete.Select2QuerySetView):
    def get_result_label(self, record):
        return '<%s> %s (%s)' % (record.id,
                                 record.name,
                                 record.get_season_display())
    
    def get_queryset(self):
        qs = Tournament.objects.all()

        ids = []

        for item in qs:
            if item.team_results.count() == 0 and \
               item.speaker_results.count() == 0:
                ids += [item.id]

        qs = qs.filter(id__in=ids)

        if self.q:
            qs = qs.filter(name__icontains=self.q)

        return qs


class ScheduleView(TemplateView):
    template_name = 'tournaments/schedule.html'

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        current_season = self.request.GET.get('season', settings.CURRENT_SEASON)
        seasons = settings.SEASONS

        context['current_season'] = current_season
        context['seasons'] = seasons

        tournaments = Tournament.objects.filter(season=current_season)

        season_display = 'UNKNOWN'

        if tournaments.count() > 0:
            season_display = tournaments.first().get_season_display()

        context['season_display'] = season_display

        months = {}

        for tournament in tournaments:
            if tournament.date.month in months:
                months[tournament.date.month] += [tournament]
            else:
                months[tournament.date.month] = [tournament]

        to_return = []

        for month, tournaments in months.items():
            to_add = {
                'month': month,
                'display': tournaments[0].date.strftime('%B'),
                'year': tournaments[0].date.year
            }

            weeks = []

            tournaments.sort(key=lambda tournament: tournament.date.day)

            current_week = {
                'date': tournaments[0].date.day,
                'one_more': (tournaments[0].date + timedelta(days=1)).day,
                'tournaments': []
            }

            for tournament in tournaments:
                if not current_week['date'] == tournament.date.day:
                    current_week['tournaments'].sort(key=lambda tournament: (1 if tournament.qual_type == 1 or tournament.qual_type == 2 else 0, tournament.qual_type))
                    weeks.append(current_week)
                    current_week = {}
                
                if not 'date' in current_week:
                    current_week['date'] = tournament.date.day
                    current_week['one_more'] = (tournament.date + timedelta(days=1)).day
                    current_week['tournaments'] = []

                current_week['tournaments'].append(tournament)

            current_week['tournaments'].sort(key=lambda tournament: (1 if tournament.qual_type == 1 or tournament.qual_type == 2 else 0, tournament.qual_type))
            weeks.append(current_week)

            weeks.sort(key=lambda week: week['date'])
            to_add['weeks'] = weeks

            to_return += [to_add]

        to_return.sort(key=lambda weeks: (weeks['year'], weeks['month']))
        context['tournaments'] = to_return

        return context


class TournamentImportWizardView(CustomMixin, SessionWizardView):
    permission_required = 'core.change_tournament'

    form_list = [
        TournamentSelectionForm,
        TournamentImportForm,
        TournamentDetailForm,
        SchoolReconciliationFormset,
        DebaterReconciliationFormset
    ]
    template_name = 'tournaments/tournament_entry.html'

    def get_template_names(self):
        if self.steps.current == '3':
            return ['tournaments/school_reconciliation.html']
        if self.steps.current == '4':
            return ['tournaments/debater_reconciliation.html']

        return super().get_template_names()


    def get_form_initial(self, step):
        storage_data = None
        tournament = None

        initial = []

        tournament = None
        
        if step == '0' and 'tournament' in self.request.GET:
            tournament = Tournament.objects.filter(id=int(self.request.GET.get('tournament'))).first()

            if tournament:
                initial = {'tournament': tournament}

        elif step != '0':
            storage_data = self.storage.get_step_data('0')
            
            tournament = Tournament.objects.get(id=storage_data.get('0-tournament'))

        if step == '2':
            storage_data = self.storage.get_step_data('1')            
            response = get_dict(storage_data.get('response'))

            initial = {
                'num_teams': get_num_teams(response['teams'],
                                           int(response['num_rounds'])),
                'num_novices': get_num_novice_debaters(response['teams'],
                                                       int(response['num_rounds']))
            }

        if step == '3':
            storage_data = self.storage.get_step_data('1')
            response = get_dict(storage_data.get('response'))

            initial = []

            for school in response['schools']:
                to_add = {
                    'id': school['id'],
                    'server_name': school['name']
                }

                school = lookup_school(school['name'].strip())

                if school:
                    to_add['school'] = school

                initial += [to_add]

        if step == '4':
            storage_data = self.storage.get_step_data('1')
            response = get_dict(storage_data.get('response'))

            storage_data = self.storage.get_step_data('3')
            schools = clean_keys(storage_data.get('schools'))

            initial = []

            for team in response['teams']:
                for debater in team['debaters']:
                    found_debater = SearchQuerySet().models(Debater).filter(
                        content=debater['name']
                    )

                    school = School.objects.filter(id=schools[team['school_id']]['school']).first()

                    found_debater = [result.object for result in found_debater.all() if result.object]
                    found_debater = [obj for obj in found_debater if \
                                     obj.school.id==schools[team['school_id']]['school'] or \
                                     obj.school.id==schools[team['hybrid_school_id']]['school']]

                    found_debater = found_debater[0] if len(found_debater) > 0 else None

                    hybrid_name = ''

                    names = schools[team['school_id']]['name']

                    if schools[team['hybrid_school_id']]['name'] != '':
                        hybrid_name = schools[team['hybrid_school_id']]['name']

                    initial += [{
                        'id': debater['id'],
                        'school_id': str(team['school_id']),
                        'server_name': debater['name'],
                        'server_school_name': names,
                        'status': 0 if debater['status'] else 1,
                        'server_hybrid_school_name': hybrid_name,
                        'school': found_debater.school if found_debater else school,
                        'debater': found_debater
                    }]

        return initial

    def get_response(self, url):
        url = url + '/json'

        request = requests.get(url)

        return request.content

    def get_form_step_data(self, form):
        to_return = form.data.copy()

        if self.steps.current == '1':
            response = self.get_response(form.cleaned_data['url'])

            to_return['response'] = response

        if self.steps.current == '3':
            storage_data = self.storage.get_step_data('1')
            response = get_dict(storage_data.get('response'))

            school_actions = {-1: {'school': -1, 'name': ''}}

            for data in form.cleaned_data:
                if not 'id' in data:
                    continue

                school_actions[int(data['id'])] = {
                    'action': CREATE if not data['school'] else LINK,
                    'id': int(data['id']),
                    'name': data['server_name'],
                    'school': data['school'].id if data['school'] else -1
                }

            to_return['schools'] = school_actions

        if self.steps.current == '4':
            storage_data = self.storage.get_step_data('1')
            response = get_dict(storage_data.get('response'))

            debater_actions = {-1: {'debater': -1, 'name': ''}}

            for data in form.cleaned_data:
                if not 'id' in data:
                    continue

                debater_actions[int(data['id'])] = {
                    'action': CREATE if not data['debater'] else LINK,
                    'id': int(data['id']),
                    'name': data['server_name'],
                    'debater': data['debater'].id if data['debater'] else -1,
                    'school': data['school'].id if data['school'] else -1,
                    'school_id': int(data['school_id']),
                    'status': int(data['status'])
                }

            to_return['debaters'] = debater_actions

        return to_return

    def done(self, form_list, form_dict, **kwargs):
        storage_data = self.storage.get_step_data('1')            
        response = get_dict(storage_data.get('response'))
        
        storage_data = self.storage.get_step_data('3')
        schools = clean_keys(storage_data.get('schools'))

        storage_data = self.storage.get_step_data('4')
        debaters = clean_keys(storage_data.get('debaters'))

        school_actions = create_schools(schools)
        debater_actions = create_debaters(school_actions, debaters)
        team_actions = create_teams(debater_actions, response['teams'])

        storage_data = self.storage.get_step_data('0')
        tournament = Tournament.objects.get(id=int(storage_data.get('0-tournament')))

        storage_data = self.storage.get_step_data('2')
        tournament.num_teams = int(storage_data.get('2-num_teams'))
        tournament.num_novice_debaters = int(storage_data.get('2-num_novices'))
        tournament.save()

        round_actions = create_rounds(team_actions, tournament, response['rounds'])
        create_round_stats(debater_actions, round_actions, tournament, response['stats'])

        create_speaker_awards(debater_actions,
                              response['speaker_results'],
                              Debater.VARSITY,
                              tournament)

        create_speaker_awards(debater_actions,
                              response['novice_speaker_results'],
                              Debater.NOVICE,
                              tournament)

        create_team_awards(team_actions,
                           response['team_results'],
                           Debater.VARSITY,
                           tournament)
        
        create_team_awards(team_actions,
                           response['novice_team_results'],
                           Debater.NOVICE,
                           tournament)
        
        return redirect(tournament.get_absolute_url())


class TournamentDataEntryWizardView(CustomMixin, SessionWizardView):
    permission_required = 'core.change_tournament'

    step_names = {
        '0': 'Tournament Selection',
        '1': 'Tournament Update',
        '2': 'Varsity Team Awards',
        '3': 'Varsity Speaker Awards',
        '4': 'Novice Team Awards',
        '5': 'Novice Speaker Awards',
    }

    form_list = [
        TournamentSelectionForm,
        TournamentDetailForm,
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
            return ['tournaments/tournament_entry.html']        
        if self.steps.current == '2':
            return ['tournaments/team_result_entry.html']
        if self.steps.current == '3':
            return ['tournaments/speaker_result_entry.html']            
        if self.steps.current == '4':
            return ['tournaments/team_result_entry.html']            
        if self.steps.current == '5':
            return ['tournaments/speaker_result_entry.html']            

        return super().get_template_names()

    def get_form_initial(self, step):
        storage_data = None
        tournament = None

        initial = []
        
        if not step == '0':
            storage_data = self.storage.get_step_data('0')
            tournament = Tournament.objects.get(id=int(storage_data.get('0-tournament')))

        if step == '0' and 'tournament' in self.request.GET:
            tournament = Tournament.objects.filter(id=int(self.request.GET.get('tournament'))).first()

            if tournament:
                initial = {'tournament': tournament}

        if step == '1':
            initial = {'num_teams': tournament.num_teams,
                       'num_novices': tournament.num_novice_debaters}


        if step == '2':
            results = TeamResult.objects.filter(tournament=tournament,
                                                type_of_place=Debater.VARSITY)

            for i in range(1, 21):
                if results.filter(place=i).exists():
                    initial += [{'debater_one': results.filter(place=i).first().team.debaters.first(),
                                 'debater_two': results.filter(place=i).first().team.debaters.last(),
                                 'ghost_points': results.filter(place=i).first().ghost_points}]
            
        if step == '3':
            results = SpeakerResult.objects.filter(tournament=tournament,
                                                type_of_place=Debater.VARSITY,)

            for i in range(1, 11):
                if results.filter(place=i).exists():
                    initial += [{'speaker': results.filter(place=i).first().debater,
                                 'tie': results.filter(place=i).first().tie}]

        if step == '4':
            results = TeamResult.objects.filter(tournament=tournament,
                                                type_of_place=Debater.NOVICE)

            for i in range(1, 11):
                if results.filter(place=i).exists():
                    initial += [{'debater_one': results.filter(place=i).first().team.debaters.first(),
                                 'debater_two': results.filter(place=i).first().team.debaters.last()}]

        if step == '5':
            results = SpeakerResult.objects.filter(tournament=tournament,
                                                   type_of_place=Debater.NOVICE)

            for i in range(1, 17):
                if results.filter(place=i).exists():
                    initial += [{'speaker': results.filter(place=i).first().debater,
                                 'tie': results.filter(place=i).first().tie}]

        return initial

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        context['title'] = self.step_names[self.steps.current]
        context['debater_form'] = DebaterForm()

        return context

    def done(self, form_list, form_dict, **kwargs):
        tournament = form_dict['0'].cleaned_data['tournament']

        tournament.num_teams = form_dict['1'].cleaned_data['num_teams']
        tournament.num_novice_debaters = form_dict['1'].cleaned_data['num_novices']

        tournament.save()

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
        for i in range(len(form_dict['2'].cleaned_data)):
            if not 'debater_one' in form_dict['2'].cleaned_data[i] or \
               not 'debater_two' in form_dict['2'].cleaned_data[i]:
                continue

            if not form_dict['2'].cleaned_data[i]['debater_one'] or \
               not form_dict['2'].cleaned_data[i]['debater_two']:
                continue

            team = get_or_create_team_for_debaters(
                form_dict['2'].cleaned_data[i]['debater_one'],
                form_dict['2'].cleaned_data[i]['debater_two']
            )

            if not team:
                continue

            place = i + 1
            type_of_place = Debater.VARSITY
            ghost_points = form_dict['2'].cleaned_data[i]['ghost_points']

            TeamResult.objects.create(tournament=tournament,
                                      team=team,
                                      type_of_place=type_of_place,
                                      place=place,
                                      ghost_points=ghost_points)

            teams_to_update += [team]

        for i in range(len(form_dict['3'].cleaned_data)):
            if not 'speaker' in form_dict['3'].cleaned_data[i]:
                continue

            place = i + 1
            type_of_place = Debater.VARSITY
            speaker = form_dict['3'].cleaned_data[i]['speaker']
            tie = form_dict['3'].cleaned_data[i]['tie']

            if not speaker:
                continue

            SpeakerResult.objects.create(tournament=tournament,
                                         debater=speaker,
                                         type_of_place=type_of_place,
                                         place=place,
                                         tie=tie)

            speakers_to_update += [speaker]


        for i in range(len(form_dict['4'].cleaned_data)):
            if not 'debater_one' in form_dict['4'].cleaned_data[i] or \
               not 'debater_two' in form_dict['4'].cleaned_data[i]:
                continue

            if not form_dict['4'].cleaned_data[i]['debater_one'] or \
               not form_dict['4'].cleaned_data[i]['debater_two']:
                continue

            team = get_or_create_team_for_debaters(
                form_dict['4'].cleaned_data[i]['debater_one'],
                form_dict['4'].cleaned_data[i]['debater_two']
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

        for i in range(len(form_dict['5'].cleaned_data)):
            if not 'speaker' in form_dict['5'].cleaned_data[i]:
                continue

            place = i + 1
            type_of_place = Debater.NOVICE
            speaker = form_dict['5'].cleaned_data[i]['speaker']
            tie = form_dict['5'].cleaned_data[i]['tie']

            if not speaker:
                continue

            SpeakerResult.objects.create(tournament=tournament,
                                         debater=speaker,
                                         type_of_place=type_of_place,
                                         place=place,
                                         tie=tie)

            novices_to_update += [speaker]

        teams_to_update = list(set(teams_to_update))
        speakers_to_update = list(set(speakers_to_update))
        novices_to_update = list(set(novices_to_update))

        if update_otys:
            for team in teams_to_update:
                update_toty(team)
                update_qual_points(team)
                update_online_quals(team)
            for debater in speakers_to_update:
                update_soty(debater)
            for debater in novices_to_update:
                update_noty(debater)

            redo_rankings(TOTY.objects.filter(season=settings.CURRENT_SEASON), season=settings.CURRENT_SEASON, cache_type='toty')
            redo_rankings(SOTY.objects.filter(season=settings.CURRENT_SEASON), season=settings.CURRENT_SEASON, cache_type='soty')
            redo_rankings(NOTY.objects.filter(season=settings.CURRENT_SEASON), season=settings.CURRENT_SEASON, cache_type='noty')
            redo_rankings(COTY.objects.filter(season=settings.CURRENT_SEASON), season=settings.CURRENT_SEASON, cache_type='coty')
            redo_rankings(OnlineQUAL.objects.filter(season=settings.CURRENT_SEASON), season=settings.CURRENT_SEASON, cache_type='online_quals')
            
        return redirect('core:tournament_detail', pk=tournament.id)
