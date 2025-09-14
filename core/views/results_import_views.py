from django.conf import settings
from django.shortcuts import redirect
from django.http import JsonResponse
from django.template.loader import render_to_string
from formtools.wizard.views import SessionWizardView

from core.forms import (
    DebaterForm, DebaterCreationFormset, SchoolForm, SchoolCreationFormset,
    TournamentDetailForm, TournamentSelectionForm, UnplacedTeamResultFormset,
    VarsitySpeakerResultFormset, VarsityTeamResultFormset, NoviceSpeakerResultFormset,
    NoviceTeamResultFormset,
)
from core.models import Team
from core.utils.team import get_or_create_team_for_debaters
from core.models.debater import Debater
from core.models.school import School
from core.models.tournament import Tournament
from core.models.results.speaker import SpeakerResult
from core.models.results.team import TeamResult
from core.models.standings.coty import COTY
from core.models.standings.noty import NOTY
from core.models.standings.online_qual import OnlineQUAL
from core.models.standings.qual import QUAL
from core.models.standings.soty import SOTY
from core.models.standings.toty import TOTY
from core.utils.api_data import APIDataHandler
from core.utils.generics import CustomMixin
from core.utils.rankings import (
    redo_rankings, update_noty, update_online_quals, update_qual_points,
    update_soty, update_toty,
)

class TournamentDataEntryWizardView(CustomMixin, SessionWizardView):
    permission_required = "core.change_tournament"
    step_names = {
        "0": "Tournament Selection", "1": "Tournament Update", "2": "Create New Schools",
        "3": "Create New Debaters", "4": "Varsity Team Awards", "5": "Varsity Speaker Awards",
        "6": "Novice Team Awards", "7": "Novice Speaker Awards", "8": "Non-placing Teams"
    }
    form_list = [
        TournamentSelectionForm, TournamentDetailForm, SchoolCreationFormset,
        DebaterCreationFormset, VarsityTeamResultFormset, VarsitySpeakerResultFormset,
        NoviceTeamResultFormset, NoviceSpeakerResultFormset, UnplacedTeamResultFormset
    ]
    template_name = "tournaments/data_entry.html"
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._api_handler = None

    def get_api_handler(self):
        if self._api_handler is None:
            self._api_handler = APIDataHandler(self.request)
        return self._api_handler

    def has_api_data(self):
        return self.get_api_handler().should_use_api_data()

    def get_template_names(self):
        templates = {
            ("0", "1"): "tournaments/tournament_entry.html",
            ("2",): "tournaments/school_creation_entry.html", 
            ("3",): "tournaments/debater_creation_entry.html",
            ("4", "6"): "tournaments/team_result_entry.html",
            ("5", "7"): "tournaments/speaker_result_entry.html",
            ("8",): "tournaments/unplaced_team_result_entry.html"
        }
        for steps, template in templates.items():
            if self.steps.current in steps:
                return [template]
        return [self.template_name]

    def get_form_initial(self, step):
        if step == "0":
            if "tournament" in self.request.GET:
                tournament = Tournament.objects.filter(id=int(self.request.GET.get("tournament"))).first()
                return {"tournament": tournament} if tournament else []
            return []
        tournament = self._get_tournament()
        if step == "1":
            return {"num_teams": tournament.num_teams, "num_novices": tournament.num_novice_debaters}
        return self._get_api_initial(step) if self.has_api_data() else self._get_db_initial(step, tournament)

    def get_form(self, step=None, data=None, files=None):
        if step is None:
            step = self.steps.current
        if self.has_api_data() and step == "2" and data is None:
            form = super().get_form(step, data, files)
            fresh_initial = self._get_api_initial(step)
            if fresh_initial:
                form.initial = fresh_initial
                form = form.__class__(initial=fresh_initial, prefix=form.prefix, **form.form_kwargs if hasattr(form, 'form_kwargs') else {})
            return form
        
        return super().get_form(step, data, files)

    def _get_tournament(self):
        if not hasattr(self, '_tournament'):
            storage_data = self.storage.get_step_data("0")
            self._tournament = Tournament.objects.get(id=int(storage_data.get("0-tournament")))
        return self._tournament

    def _get_api_initial(self, step):
        handler = self.get_api_handler()
        if step == "2":
            return handler.get_new_schools_from_api()
        elif step == "3":
            return handler.get_new_debaters_from_api()
        elif step in ["4", "5", "6", "7", "8"]:
            endpoints = {
                "4": 'varsity-team-placements', "5": 'varsity-speaker-awards', 
                "6": 'novice-team-placements', "7": 'novice-speaker-awards', "8": 'non-placing-teams'
            }
            endpoint = endpoints[step]
            return (handler.get_teams_from_api(endpoint) if step in ["4", "6", "8"] 
                   else handler.get_speakers_from_api(endpoint))
        return []

    def _get_db_initial(self, step, tournament):
        configs = {
            "4": (Debater.VARSITY, "team", {"place__gt": 0}),
            "5": (Debater.VARSITY, "speaker", {"place__gt": 0}),
            "6": (Debater.NOVICE, "team", {"place__gt": 0}), 
            "7": (Debater.NOVICE, "speaker", {"place__gt": 0}),
            "8": (Debater.VARSITY, "team", {"place": -1})
        }
        if step not in configs:
            return []
        type_of_place, result_type, place_filter = configs[step]
        
        if result_type == "speaker":
            results = SpeakerResult.objects.filter(
                tournament=tournament, type_of_place=type_of_place, **place_filter
            ).select_related('debater', 'debater__school').order_by("place")
            return [{"speaker": r.debater, "tie": r.tie} for r in results]
        
        results = TeamResult.objects.filter(
            tournament=tournament, type_of_place=type_of_place, **place_filter
        ).select_related('team').prefetch_related('team__debaters__school').order_by("place")
        
        initial = []
        for result in results:
            debaters = list(result.team.debaters.all())
            team_data = {
                "debater_one": debaters[0] if debaters else None,
                "debater_two": debaters[1] if len(debaters) > 1 else None,
            }
            if type_of_place == Debater.VARSITY and result.place > 0:
                team_data["ghost_points"] = result.ghost_points
            initial.append(team_data)
        return initial

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        context.update({
            "title": self.step_names[self.steps.current],
            "debater_form": DebaterForm(),
            "school_form": SchoolForm(),
            "has_api_data": self.has_api_data()
        })
        return context

    def process_step(self, form):
        step = self.steps.current
        if not self.has_api_data():
            return super().process_step(form)
            
        if step == "2":
            school_data = [{'name': fd['name'], 'included_in_oty': fd.get('included_in_oty', True)} 
                          for fd in form.cleaned_data if fd.get('name')]
            if school_data:
                self.get_api_handler().create_schools_from_data(school_data)
        elif step == "3":
            debater_data = [{'first_name': fd['first_name'], 'last_name': fd['last_name'], 
                           'school': fd['school'], 'tournament_id': fd.get('tournament_id')}
                          for fd in form.cleaned_data 
                          if fd.get('first_name') and fd.get('last_name') and fd.get('school')]
            if debater_data:
                self.get_api_handler().create_debaters_from_data(debater_data)
        return super().process_step(form)

    def done(self, form_list, form_dict):
        tournament = form_dict["0"].cleaned_data["tournament"]
        tournament.num_teams = form_dict["1"].cleaned_data["num_teams"]
        tournament.num_novice_debaters = form_dict["1"].cleaned_data["num_novices"]
        tournament.save()

        teams_to_update, speakers_to_update, novices_to_update = [], [], []
        TeamResult.objects.filter(tournament=tournament).delete()
        SpeakerResult.objects.filter(tournament=tournament).delete()
        QUAL.objects.filter(tournament=tournament).delete()

        self._create_team_results(tournament, form_dict["4"], Debater.VARSITY, teams_to_update, True)
        self._create_team_results(tournament, form_dict["6"], Debater.NOVICE, teams_to_update, False)
        self._create_team_results(tournament, form_dict["8"], Debater.VARSITY, teams_to_update, False, -1)
        self._create_speaker_results(tournament, form_dict["5"], Debater.VARSITY, speakers_to_update)
        self._create_speaker_results(tournament, form_dict["7"], Debater.NOVICE, novices_to_update)
        self._update_rankings(tournament, teams_to_update, speakers_to_update, novices_to_update)
        
        if hasattr(self.request, 'session') and 'tournament_api_url' in self.request.session:
            del self.request.session['tournament_api_url']
        return redirect("core:tournament_detail", pk=tournament.id)

    def _create_team_results(self, tournament, form_data, type_of_place, teams_to_update, has_ghost_points=False, place=None):
        results_to_create = []
        for i, team_data in enumerate(form_data.cleaned_data):
            debater_one = team_data.get("debater_one")
            debater_two = team_data.get("debater_two")
            if not (debater_one and debater_two):
                continue
            team = get_or_create_team_for_debaters(debater_one, debater_two)
            teams_to_update.append(team)
            result_data = {
                'tournament': tournament, 'team': team, 'type_of_place': type_of_place,
                'place': place if place is not None else team_data.get("ORDER", i + 1)
            }
            if has_ghost_points:
                result_data['ghost_points'] = team_data.get("ghost_points", 0)
            results_to_create.append(TeamResult(**result_data))
        if results_to_create:
            TeamResult.objects.bulk_create(results_to_create)

    def _create_speaker_results(self, tournament, form_data, type_of_place, speakers_to_update):
        results_to_create = []
        for i, speaker_data in enumerate(form_data.cleaned_data):
            speaker = speaker_data.get("speaker")
            if not speaker:
                continue
            results_to_create.append(SpeakerResult(
                tournament=tournament, debater=speaker, type_of_place=type_of_place,
                place=speaker_data.get("ORDER", i + 1), tie=speaker_data.get("tie", False)
            ))
            speakers_to_update.append(speaker)
        if results_to_create:
            SpeakerResult.objects.bulk_create(results_to_create)

    def _update_rankings(self, tournament, teams_to_update, speakers_to_update, novices_to_update):
        if settings.CURRENT_SEASON != tournament.season:
            return
        teams_to_update = list(set(filter(None, teams_to_update)))
        speakers_to_update = list(set(filter(None, speakers_to_update)))
        novices_to_update = list(set(filter(None, novices_to_update)))
        if not (teams_to_update or speakers_to_update or novices_to_update):
            return

        for team in teams_to_update:
            update_toty(team)
            update_qual_points(team)
            update_online_quals(team)
        for debater in speakers_to_update:
            update_soty(debater)
        for debater in novices_to_update:
            update_noty(debater)

        rankings_to_update = [
            (TOTY, "toty"), (SOTY, "soty"), (NOTY, "noty"), 
            (COTY, "coty"), (OnlineQUAL, "online_quals")
        ]
        for model, cache_type in rankings_to_update:
            redo_rankings(model.objects.filter(season=settings.CURRENT_SEASON),
                         season=settings.CURRENT_SEASON, cache_type=cache_type)




def get_new_team_form(request):
    if request.method != 'GET':
        return JsonResponse({'error': 'Invalid request'}, status=400)
    form_index = int(request.GET.get('form_index', 0))
    form_type = request.GET.get('form_type', 'team')
    form_config = {
        'team': (VarsityTeamResultFormset, '4'), 'speaker': (VarsitySpeakerResultFormset, '5'),
        'school': (SchoolCreationFormset, '2'), 'debater': (DebaterCreationFormset, '3')
    }
    FormsetClass, step_prefix = form_config.get(form_type, (VarsitySpeakerResultFormset, '5'))
    empty_form = FormsetClass().empty_form
    empty_form.prefix = f'{step_prefix}-{form_index}'
    if hasattr(empty_form, 'fields') and 'ORDER' in empty_form.fields:
        empty_form.initial = {'ORDER': form_index + 1}
    html = render_to_string('tournaments/ajax_form_row.html', {
        'form': empty_form, 'form_index': form_index, 'place_number': form_index + 1, 'form_type': form_type
    })
    return JsonResponse({'html': html})
