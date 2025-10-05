from django.conf import settings
from django.shortcuts import redirect
from django.http import JsonResponse
from django.template.loader import render_to_string
from formtools.wizard.views import SessionWizardView

from core.forms import (
    DebaterForm, DebaterCreationFormset, SchoolForm, SchoolCreationFormset,
    UnplacedTeamResultFormset, VarsitySpeakerResultFormset, VarsityTeamResultFormset,
    NoviceSpeakerResultFormset, NoviceTeamResultFormset,
)
from core.utils.team import get_or_create_team_for_debaters
from core.models.debater import Debater
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
        "0": "Create New Schools",
        "1": "Create New Debaters",
        "2": "Varsity Team Awards",
        "3": "Varsity Speaker Awards",
        "4": "Novice Team Awards",
        "5": "Novice Speaker Awards",
        "6": "Non-placing Teams"
    }

    step_configs = {
        "0": {"type": "school", "name": "School", "has_ghost_points": False},
        "1": {"type": "debater", "name": "Debater", "has_ghost_points": False},
        "2": {"type": "team", "name": "Team", "has_ghost_points": True},
        "3": {"type": "speaker", "name": "Speaker", "has_ghost_points": False},
        "4": {"type": "team", "name": "Team", "has_ghost_points": False},
        "5": {"type": "speaker", "name": "Speaker", "has_ghost_points": False},
        "6": {"type": "team", "name": "Unplaced Team", "is_unplaced": True, "has_ghost_points": False},
    }

    form_list = [
        SchoolCreationFormset,
        DebaterCreationFormset,
        VarsityTeamResultFormset,
        VarsitySpeakerResultFormset,
        NoviceTeamResultFormset,
        NoviceSpeakerResultFormset,
        UnplacedTeamResultFormset
    ]
    template_name = "tournaments/data_entry.html"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._api_handler = None
        self._tournament = None

    def dispatch(self, request, *args, **kwargs):
        # Get the tournament ID from URL parameters
        current_tournament_id = request.GET.get("tournament")
        if current_tournament_id:
            current_tournament_id = int(current_tournament_id)

        # Check if we have session data for a different tournament
        api_handler = APIDataHandler(request)
        session_tournament_id = api_handler.get_tournament_id()

        # If tournament IDs don't match, clear stale session data
        if session_tournament_id and session_tournament_id != current_tournament_id:
            APIDataHandler.clear_tournament_session_data(request)

        if current_tournament_id:
            api_handler.set_tournament_id(current_tournament_id)

        return super().dispatch(request, *args, **kwargs)

    def render_done(self, form, **kwargs):
        """
        Override the render_done method to bypass validation for step 0 (SchoolCreationFormset)
        when using API data. This ensures we don't get validation errors for already created schools.
        """
        if self.has_api_data():
            form_dict = {}
            form_list = []

            for step in self.get_form_list():
                if step == '0':
                    form_obj = self.get_form(step=step, data=self.storage.get_step_data(step),
                                           files=self.storage.get_step_files(step))
                    form_obj.is_valid = lambda: True
                    form_list.append(form_obj)
                    form_dict[step] = form_obj
                    continue

                form_obj = self.get_form(step=step, data=self.storage.get_step_data(step),
                                       files=self.storage.get_step_files(step))
                if not form_obj.is_valid():
                    return self.render_revalidation_failure(step, form_obj, **kwargs)
                form_list.append(form_obj)
                form_dict[step] = form_obj

            return self.done(form_list, form_dict)

        return super().render_done(form, **kwargs)

    def get_api_handler(self):
        if self._api_handler is None:
            self._api_handler = APIDataHandler(self.request)
        return self._api_handler

    def has_api_data(self):
        return self.get_api_handler().should_use_api_data()

    def get_form_initial(self, step):
        tournament = self._get_tournament()
        return self._get_api_initial(step) if self.has_api_data() else self._get_db_initial(step, tournament)

    def get_form(self, step=None, data=None, files=None):
        if step is None:
            step = self.steps.current
        if self.has_api_data() and step == "0" and data is None:
            form = super().get_form(step, data, files)
            fresh_initial = self._get_api_initial(step)
            if fresh_initial:
                form.initial = fresh_initial
                form = form.__class__(initial=fresh_initial, prefix=form.prefix, **form.form_kwargs if hasattr(form, 'form_kwargs') else {})
            return form

        return super().get_form(step, data, files)

    def _get_tournament(self):
        if not hasattr(self, '_tournament') or self._tournament is None:
            tournament_id = self.request.GET.get("tournament")

            if not tournament_id:
                api_handler = self.get_api_handler()
                tournament_id = api_handler.get_tournament_id()

            if not tournament_id:
                raise ValueError("Tournament ID must be provided as a URL parameter or session")

            self._tournament = Tournament.objects.get(id=int(tournament_id))
        return self._tournament

    def _get_api_initial(self, step):
        handler = self.get_api_handler()
        if step == "0":
            return handler.get_new_schools_from_api()
        if step == "1":
            return handler.get_new_debaters_from_api()
        if step in ["2", "3", "4", "5", "6"]:
            endpoints = {
                "2": 'varsity-team-placements', "3": 'varsity-speaker-awards',
                "4": 'novice-team-placements', "5": 'novice-speaker-awards', "6": 'non-placing-teams'
            }
            endpoint = endpoints[step]
            return (handler.get_teams_from_api(endpoint) if step in ["2", "4", "6"]
                   else handler.get_speakers_from_api(endpoint))
        return []

    def _get_db_initial(self, step, tournament):
        configs = {
            "2": (Debater.VARSITY, "team", {"place__gt": 0}),
            "3": (Debater.VARSITY, "speaker", {"place__gt": 0}),
            "4": (Debater.NOVICE, "team", {"place__gt": 0}),
            "5": (Debater.NOVICE, "speaker", {"place__gt": 0}),
            "6": (Debater.VARSITY, "team", {"place": -1})
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

        current_step = self.steps.current
        step_config = self.step_configs.get(current_step, {"type": "item", "name": "Item"})

        context.update({
            "title": self.step_names[self.steps.current],
            "debater_form": DebaterForm(),
            "school_form": SchoolForm(),
            "has_api_data": self.has_api_data(),
            "step_config": step_config
        })
        return context

    def process_step(self, form):
        step = self.steps.current
        if not self.has_api_data():
            return super().process_step(form)

        if step == "0":
            school_data = []
            school_mapping = {}
            
            for fd in form.cleaned_data:
                if not fd.get('name'):
                    continue
                    
                # Check if user wants to link to an existing school
                if fd.get('existing_school'):
                    # Create a SchoolLookup to map the new name to the existing school
                    from core.models.school import SchoolLookup
                    existing_school = fd['existing_school']
                    school_name = fd['name']
                    
                    # Create or update the lookup
                    lookup, created = SchoolLookup.objects.update_or_create(
                        server_name=school_name,
                        defaults={'school': existing_school}
                    )
                    school_mapping[school_name] = existing_school
                else:
                    # Create a new school as usual
                    school_data.append({
                        'name': fd['name'], 
                        'included_in_oty': fd.get('included_in_oty', True)
                    })
            
            if school_data:
                self.get_api_handler().create_schools_from_data(school_data)
        elif step == "1":
            debater_data = [{'first_name': fd['first_name'], 'last_name': fd['last_name'],
                           'school': fd['school'], 'tournament_id': fd.get('tournament_id')}
                          for fd in form.cleaned_data
                          if fd.get('first_name') and fd.get('last_name') and fd.get('school')]
            if debater_data:
                self.get_api_handler().create_debaters_from_data(debater_data)
        return super().process_step(form)

    def done(self, form_list, form_dict):
        tournament = self._get_tournament()

        teams_to_update, speakers_to_update, novices_to_update = [], [], []
        TeamResult.objects.filter(tournament=tournament).delete()
        SpeakerResult.objects.filter(tournament=tournament).delete()
        QUAL.objects.filter(tournament=tournament).delete()

        self._create_team_results(tournament, form_dict["2"], Debater.VARSITY, teams_to_update, has_ghost_points=True)
        self._create_team_results(tournament, form_dict["4"], Debater.NOVICE, teams_to_update)
        self._create_team_results(tournament, form_dict["6"], Debater.VARSITY, teams_to_update, place=-1)
        self._create_speaker_results(tournament, form_dict["3"], Debater.VARSITY, speakers_to_update)
        self._create_speaker_results(tournament, form_dict["5"], Debater.NOVICE, novices_to_update)
        self._update_rankings(tournament, teams_to_update, speakers_to_update, novices_to_update)

        # Clear tournament session data when done
        APIDataHandler.clear_tournament_session_data(self.request)
        return redirect("core:tournament_detail", pk=tournament.id)

    def _create_team_results(self, tournament, form_data, type_of_place, teams_to_update, **kwargs):
        has_ghost_points = kwargs.get('has_ghost_points', False)
        place = kwargs.get('place', None)
        results_to_create = []

        if not hasattr(form_data, 'cleaned_data') or not form_data.cleaned_data:
            return

        for i, team_data in enumerate(form_data.cleaned_data):
            if team_data.get('DELETE'):
                continue
                
            debater_one = team_data.get("debater_one")
            debater_two = team_data.get("debater_two")
            if not (debater_one and debater_two):
                continue
            team = get_or_create_team_for_debaters(debater_one, debater_two)
            teams_to_update.append(team)
            final_place = place if place is not None else team_data.get("ORDER", i + 1)
            result_data = {
                'tournament': tournament,
                'team': team,
                'type_of_place': type_of_place,
                'place': final_place
            }
            if has_ghost_points:
                result_data['ghost_points'] = team_data.get("ghost_points", 0)

            if tournament is None:
                raise ValueError(f"Tournament is None when creating TeamResult for team {team.id}")

            results_to_create.append(TeamResult(**result_data))
        if results_to_create:
            TeamResult.objects.bulk_create(results_to_create)

    def _create_speaker_results(self, tournament, form_data, type_of_place, speakers_to_update):
        results_to_create = []
        
        if not hasattr(form_data, 'cleaned_data') or not form_data.cleaned_data:
            return
            
        for i, speaker_data in enumerate(form_data.cleaned_data):
            if speaker_data.get('DELETE'):
                continue
                
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
    has_ghost_points = request.GET.get('has_ghost_points') in {'1', 'true', 'True'}
    item_name = request.GET.get('item_name') or (form_type.replace('_', ' ').title() if form_type else 'Item')
    form_config = {
        'team': (VarsityTeamResultFormset, '2'),
        'speaker': (VarsitySpeakerResultFormset, '3'),
        'school': (SchoolCreationFormset, '0'),
        'debater': (DebaterCreationFormset, '1')
    }
    FormsetClass, step_prefix = form_config.get(form_type, (VarsitySpeakerResultFormset, '3'))
    empty_form = FormsetClass().empty_form
    empty_form.prefix = f'{step_prefix}-{form_index}'
    if hasattr(empty_form, 'fields') and 'ORDER' in empty_form.fields:
        empty_form.initial = {'ORDER': form_index + 1}
    html = render_to_string('tournaments/includes/formset_row.html', {
        'form': empty_form,
        'form_index': form_index,
        'place_number': form_index + 1,
        'form_type': form_type,
        'has_ghost_points': has_ghost_points,
        'item_name': item_name,
    })
    return JsonResponse({'html': html})
