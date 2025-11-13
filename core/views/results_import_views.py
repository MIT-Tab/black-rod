import json
from django.conf import settings
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.db import transaction
from django.shortcuts import redirect, render
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.views import View

from haystack import connections
from haystack.exceptions import NotHandled

from core.forms import (
    DebaterForm, DebaterCreationFormset, SchoolForm, SchoolCreationFormset,
    UnplacedTeamResultFormset, VarsitySpeakerResultFormset, VarsityTeamResultFormset,
    NoviceSpeakerResultFormset, NoviceTeamResultFormset,
)
from core.utils.team import get_or_create_team_for_debaters
from core.models.debater import Debater
from core.models.tournament import Tournament
from core.models.school import School, SchoolLookup
from core.models.results.speaker import SpeakerResult
from core.models.results.team import TeamResult
from core.models.standings.coty import COTY
from core.models.standings.noty import NOTY
from core.models.standings.online_qual import OnlineQUAL
from core.models.standings.qual import QUAL
from core.models.standings.soty import SOTY
from core.models.standings.toty import TOTY
from core.utils.api_data import APIDataHandler
from core.utils.rankings import (
    redo_rankings, update_noty, update_online_quals, update_qual_points,
    update_soty, update_toty,
)

class FormsetValidationError(Exception):
    def __init__(self, context):
        super().__init__("Form validation failed")
        self.context = context


class TournamentDataEntryView(PermissionRequiredMixin, View):
    permission_required = "core.change_tournament"
    template_name = "tournaments/data_entry.html"
    
    formset_config = {
        "schools": (SchoolCreationFormset, "schools"),
        "debaters": (DebaterCreationFormset, "debaters"),
        "varsity_teams": (VarsityTeamResultFormset, "varsity_teams"),
        "varsity_speakers": (VarsitySpeakerResultFormset, "varsity_speakers"),
        "novice_teams": (NoviceTeamResultFormset, "novice_teams"),
        "novice_speakers": (NoviceSpeakerResultFormset, "novice_speakers"),
        "unplaced_teams": (UnplacedTeamResultFormset, "unplaced_teams"),
    }
    _tournament = None
    _api_handler = None
    tab_labels = {
        "schools": "Schools",
        "debaters": "Debaters",
        "varsity_teams": "Varsity Teams",
        "varsity_speakers": "Varsity Speakers",
        "novice_teams": "Novice Teams",
        "novice_speakers": "Novice Speakers",
        "unplaced_teams": "Non-placing Teams",
    }

    def __init__(self, *args, **kwargs):
        if "_api_handler" in kwargs:
            self._api_handler = kwargs.pop("_api_handler")
        if "_tournament" in kwargs:
            self._tournament = kwargs.pop("_tournament")
        super().__init__(*args, **kwargs)

    def get_api_handler(self):
        if self._api_handler is None:
            self._api_handler = APIDataHandler(self.request)
            api_url = self.request.GET.get('api_url') or self.request.POST.get('api_url')
            if api_url:
                self._api_handler.set_api_url(api_url)
        return self._api_handler

    def has_api_data(self):
        return self.get_api_handler().should_use_api_data()
    
    def _build_context(self, tournament, formsets, has_api_data, **extra):
        context = {
            "tournament": tournament,
            "formsets": formsets,
            "debater_form": DebaterForm(),
            "school_form": SchoolForm(),
            "has_api_data": has_api_data,
            "current_api_url": self.get_api_handler().get_api_url(),
        }
        context.update(extra)
        return context

    def _get_tournament(self):
        if not hasattr(self, '_tournament') or self._tournament is None:
            tournament_id = self.request.GET.get("tournament") or self.request.POST.get("tournament")

            if not tournament_id:
                raise ValueError("Tournament ID must be provided as a URL parameter")

            self._tournament = Tournament.objects.get(id=int(tournament_id))
        return self._tournament

    def get(self, request, *args, **kwargs):
        tournament = self._get_tournament()
        
        formsets = {}
        for tab_key, (formset_class, prefix) in self.formset_config.items():
            initial_data = self._get_initial_data(tab_key, tournament)
            formsets[tab_key] = formset_class(
                initial=initial_data,
                prefix=prefix
            )
        
        new_entities_json = "{}"
        has_api = self.has_api_data()
        if has_api:
            new_entities_json = self._prepare_new_entities_json(formsets)
            self._inject_new_debater_choices(formsets)
        
        context = self._build_context(
            tournament,
            formsets,
            has_api,
            new_entities_json=new_entities_json,
        )

        return render(request, self.template_name, context)

    def _prepare_new_entities_json(self, formsets):
        new_schools = {}
        new_debaters = {}
        
        if 'schools' in formsets:
            for i, form in enumerate(formsets['schools'].forms):
                if form.initial:
                    name = form.initial.get('name')
                    server_name = form.initial.get('server_name', name)
                    if name and server_name:
                        new_schools[server_name] = {
                            'name': name,
                            'server_name': server_name
                        }

        if 'debaters' in formsets:
            for i, form in enumerate(formsets['debaters'].forms):
                if form.initial:
                    tournament_id = form.initial.get('tournament_id')
                    first_name = form.initial.get('first_name')
                    last_name = form.initial.get('last_name')
                    school_name = form.initial.get('school_name')
                    
                    if tournament_id and first_name and last_name:
                        new_debaters[str(tournament_id)] = {
                            'first_name': first_name,
                            'last_name': last_name,
                            'tournament_id': tournament_id,
                            'school_name': school_name,
                            'name': f"{first_name} {last_name}"
                        }

        result = json.dumps({'schools': new_schools, 'debaters': new_debaters})
        
        return result

    def _inject_new_debater_choices(self, formsets):
        new_debaters_by_tid = {}
        if 'debaters' in formsets:
            for i, form in enumerate(formsets['debaters'].forms):
                if form.initial:
                    tid = form.initial.get('tournament_id')
                    first_name = form.initial.get('first_name')
                    last_name = form.initial.get('last_name')
                    if tid and first_name and last_name:
                        new_debaters_by_tid[str(tid)] = {
                            'name': f"{first_name} {last_name}",
                            'temp_id': f"temp_tid_{tid}"
                        }
        
        new_schools_by_name = {}
        if 'schools' in formsets:
            for i, form in enumerate(formsets['schools'].forms):
                if form.initial:
                    name = form.initial.get('name')
                    server_name = form.initial.get('server_name', name)
                    if name and server_name:
                        new_schools_by_name[server_name] = {
                            'name': name,
                            'server_name': server_name,
                            'temp_id': f"temp_school_{server_name.replace(' ', '_')}"
                        }

        if 'debaters' in formsets:
            injected_count = 0
            for form in formsets['debaters'].forms:
                if not form.initial:
                    continue
                
                school_name = form.initial.get('school_name')
                if school_name and school_name in new_schools_by_name and not form.initial.get('school'):
                    school_info = new_schools_by_name[school_name]
                    temp_id = school_info['temp_id']
                    name = school_info['name']
                    
                    form.fields['school'].widget.attrs['data-new-entity-name'] = name
                    form.fields['school'].widget.attrs['data-new-entity-id'] = temp_id
                    injected_count += 1
        
        team_formset_keys = ['varsity_teams', 'novice_teams', 'unplaced_teams']
        for key in team_formset_keys:
            if key not in formsets:
                continue
            
            injected_one_count = 0
            injected_two_count = 0
            
            for i, form in enumerate(formsets[key].forms):
                if not form.initial:
                    continue
                
                tid_one = form.initial.get('debater_one_tournament_id')
                debater_one_exists = form.initial.get('debater_one')
                
                if tid_one and str(tid_one) in new_debaters_by_tid and not debater_one_exists:
                    debater_info = new_debaters_by_tid[str(tid_one)]
                    temp_id = debater_info['temp_id']
                    name = debater_info['name']
                    
                    form.fields['debater_one'].widget.attrs['data-new-entity-name'] = name
                    form.fields['debater_one'].widget.attrs['data-new-entity-id'] = temp_id
                    injected_one_count += 1
                
                tid_two = form.initial.get('debater_two_tournament_id')
                debater_two_exists = form.initial.get('debater_two')
                
                if tid_two and str(tid_two) in new_debaters_by_tid and not debater_two_exists:
                    debater_info = new_debaters_by_tid[str(tid_two)]
                    temp_id = debater_info['temp_id']
                    name = debater_info['name']
                    
                    form.fields['debater_two'].widget.attrs['data-new-entity-name'] = name
                    form.fields['debater_two'].widget.attrs['data-new-entity-id'] = temp_id
                    injected_two_count += 1
        
        speaker_formset_keys = ['varsity_speakers', 'novice_speakers']
        for key in speaker_formset_keys:
            if key not in formsets:
                continue
                
            for form in formsets[key].forms:
                if not form.initial:
                    continue
                
                tid = form.initial.get('tournament_id')
                if tid and str(tid) in new_debaters_by_tid and not form.initial.get('speaker'):
                    debater_info = new_debaters_by_tid[str(tid)]
                    temp_id = debater_info['temp_id']
                    name = debater_info['name']
                    
                    form.fields['speaker'].widget.attrs['data-new-entity-name'] = name
                    form.fields['speaker'].widget.attrs['data-new-entity-id'] = temp_id

    def post(self, request, *args, **kwargs):
        tournament = self._get_tournament()
        has_api = self.has_api_data()
        try:
            with transaction.atomic():
                return self._handle_post_request(request, tournament, has_api)
        except FormsetValidationError as exc:
            return render(request, self.template_name, exc.context)

    def _handle_post_request(self, request, tournament, has_api):
        
        formsets = {}
        created_schools = {}
        created_debaters = {}
        temp_school_id_map = {}
        temp_debater_id_map = {}
        modified_post = request.POST
        
        if has_api:
            schools_formset = SchoolCreationFormset(request.POST, prefix='schools')
            if not schools_formset.is_valid():
                formsets = {'schools': schools_formset}
                for tab_key, (formset_class, prefix) in self.formset_config.items():
                    if tab_key != 'schools':
                        formsets[tab_key] = formset_class(request.POST, prefix=prefix)
                
                context = self._build_context(tournament, formsets, has_api)
                self._annotate_error_context(context, "schools")
                raise FormsetValidationError(context)
            
            created_schools = self._process_schools(schools_formset)
            temp_school_id_map = self._build_temp_school_id_map(created_schools)
            
            modified_post = self._replace_temp_ids_in_post(request.POST, temp_school_id_map)
            
            formsets = {'schools': schools_formset}
            debaters_formset = DebaterCreationFormset(modified_post, prefix='debaters')
            
            is_valid = debaters_formset.is_valid()
            
            if not is_valid:
                for tab_key, (formset_class, prefix) in self.formset_config.items():
                    if tab_key not in ['schools', 'debaters']:
                        initial_data = self._get_initial_data(tab_key, tournament)
                        formsets[tab_key] = formset_class(initial=initial_data, prefix=prefix)
                formsets['debaters'] = debaters_formset
                
                context = self._build_context(tournament, formsets, has_api)
                self._annotate_error_context(context, "debaters")
                raise FormsetValidationError(context)
            
            formsets['debaters'] = debaters_formset
            
            created_debaters = self._process_debaters(debaters_formset, created_schools)
            
            temp_debater_id_map = self._build_temp_debater_id_map(created_debaters)
            
            if temp_debater_id_map:
                modified_post = self._replace_temp_debater_ids_in_post(modified_post, temp_debater_id_map)
        
        all_valid = True
        invalid_tabs = []
        for tab_key in ['varsity_teams', 'novice_teams', 'unplaced_teams', 'varsity_speakers', 'novice_speakers']:
            if tab_key in self.formset_config:
                formset_class, prefix = self.formset_config[tab_key]
                formset = formset_class(modified_post, prefix=prefix)
                formsets[tab_key] = formset
                
                if not formset.is_valid():
                    all_valid = False
                    invalid_tabs.append(tab_key)
        
        if not all_valid:
            context = self._build_context(tournament, formsets, has_api)
            if invalid_tabs:
                self._annotate_error_context(context, invalid_tabs[0])
            raise FormsetValidationError(context)
        
        try:
            if has_api:
                self._update_search_index(created_schools, created_debaters)
            
            TeamResult.objects.filter(tournament=tournament).delete()
            SpeakerResult.objects.filter(tournament=tournament).delete()
            QUAL.objects.filter(tournament=tournament).delete()
            
            teams_to_update = []
            speakers_to_update = []
            novices_to_update = []
            
            self._create_team_results(
                tournament, formsets["varsity_teams"], Debater.VARSITY, 
                teams_to_update, has_ghost_points=True
            )
            self._create_team_results(
                tournament, formsets["novice_teams"], Debater.NOVICE, teams_to_update
            )
            self._create_team_results(
                tournament, formsets["unplaced_teams"], Debater.VARSITY, 
                teams_to_update, place=-1
            )
            
            self._create_speaker_results(
                tournament, formsets["varsity_speakers"], Debater.VARSITY, speakers_to_update
            )
            self._create_speaker_results(
                tournament, formsets["novice_speakers"], Debater.NOVICE, novices_to_update
            )
            
            self._update_rankings(tournament, teams_to_update, speakers_to_update, novices_to_update)
            
            self._reindex_debaters(teams_to_update, speakers_to_update, novices_to_update)
        
        except Exception as e:
            import traceback
            traceback.print_exc()
            
            from django.forms import ValidationError
            
            for tab_key in ['varsity_teams', 'novice_teams', 'unplaced_teams', 'varsity_speakers', 'novice_speakers']:
                if tab_key not in formsets and tab_key in self.formset_config:
                    formset_class, prefix = self.formset_config[tab_key]
                    initial_data = self._get_initial_data(tab_key, tournament)
                    formsets[tab_key] = formset_class(initial=initial_data, prefix=prefix)
            
            if has_api and 'schools' in formsets:
                formsets["schools"]._non_form_errors = [ValidationError(f"Error processing data: {str(e)}")]
                context = self._build_context(tournament, formsets, has_api)
                self._annotate_error_context(context, "schools")
            elif 'varsity_teams' in formsets:
                formsets["varsity_teams"]._non_form_errors = [ValidationError(f"Error processing data: {str(e)}")]
                context = self._build_context(tournament, formsets, has_api)
                self._annotate_error_context(context, "varsity_teams")
            else:
                context = self._build_context(tournament, formsets, has_api)
            raise FormsetValidationError(context)
        
        return redirect("core:tournament_detail", pk=tournament.id)

    def _get_initial_data(self, tab_key, tournament):
        if self.has_api_data():
            return self._get_api_initial(tab_key)
        return self._get_db_initial(tab_key, tournament)

    def _get_api_initial(self, tab_key):
        handler = self.get_api_handler()
        
        api_mapping = {
            "schools": handler.get_new_schools_from_api,
            "debaters": handler.get_new_debaters_from_api,
            "varsity_teams": lambda: handler.get_teams_from_api('varsity-team-placements'),
            "varsity_speakers": lambda: handler.get_speakers_from_api('varsity-speaker-awards'),
            "novice_teams": lambda: handler.get_teams_from_api('novice-team-placements'),
            "novice_speakers": lambda: handler.get_speakers_from_api('novice-speaker-awards'),
            "unplaced_teams": lambda: handler.get_teams_from_api('non-placing-teams')
        }
        
        if tab_key in api_mapping:
            return api_mapping[tab_key]()
        return []

    def _get_db_initial(self, tab_key, tournament):
        if tab_key in ["schools", "debaters"]:
            return []
        
        db_mapping = {
            "varsity_teams": (Debater.VARSITY, "team", {"place__gt": 0}),
            "varsity_speakers": (Debater.VARSITY, "speaker", {"place__gt": 0}),
            "novice_teams": (Debater.NOVICE, "team", {"place__gt": 0}),
            "novice_speakers": (Debater.NOVICE, "speaker", {"place__gt": 0}),
            "unplaced_teams": (Debater.VARSITY, "team", {"place": -1})
        }
        
        if tab_key not in db_mapping:
            return []
        
        type_of_place, result_type, place_filter = db_mapping[tab_key]
        
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

    def _process_schools(self, formset):
        from core.models.school import School
        
        if not formset.is_valid():
            return {}
        
        created_schools = {}
        school_data_for_api = []
        
        for form_data in formset.cleaned_data:
            if not form_data or not form_data.get('name'):
                continue
            
            school_name = form_data['name']
            server_name = form_data.get('server_name', school_name)
            
            if form_data.get('existing_school'):
                existing_school = form_data['existing_school']
                
                if self.has_api_data():
                    SchoolLookup.objects.update_or_create(
                        server_name=server_name,
                        defaults={'school': existing_school}
                    )
                    self.get_api_handler().link_tournament_school(server_name, existing_school)
                
                created_schools[school_name] = existing_school
                created_schools[server_name] = existing_school
            else:
                if self.has_api_data():
                    school_data_for_api.append({
                        'name': school_name,
                        'server_name': server_name,
                        'included_in_oty': form_data.get('included_in_oty', True)
                    })
                else:
                    school, created = School.objects.get_or_create(
                        name=school_name,
                        defaults={'included_in_oty': form_data.get('included_in_oty', True)}
                    )
                    created_schools[school_name] = school
                    created_schools[server_name] = school
        
        if school_data_for_api and self.has_api_data():
            api_created_qs = self.get_api_handler().create_schools_from_data(school_data_for_api)
            if api_created_qs:
                for school_info in school_data_for_api:
                    school = School.objects.filter(name=school_info['name']).first()
                    if school:
                        created_schools[school_info['name']] = school
                        created_schools[school_info['server_name']] = school
                        self.get_api_handler().link_tournament_school(school_info['server_name'], school)
        
        if self.has_api_data():
            handler = self.get_api_handler()
            for school_name, school_id in handler._school_name_map.items():
                if school_name not in created_schools:
                    try:
                        school = School.objects.get(id=school_id)
                        created_schools[school_name] = school
                    except School.DoesNotExist:
                        pass
        
        return created_schools

    def _build_temp_school_id_map(self, created_schools):
        temp_id_map = {}
        seen_school_ids = set()
        
        for key, school in created_schools.items():
            if not school or school.id in seen_school_ids:
                continue
            
            temp_id = f"temp_school_{key.replace(' ', '_')}"
            temp_id_map[temp_id] = str(school.id)
            seen_school_ids.add(school.id)
        
        return temp_id_map

    def _replace_temp_ids_in_post(self, post_data, temp_school_id_map):
        from core.models.school import School
        
        if not temp_school_id_map:
            modified = post_data.copy()
            if hasattr(modified, '_mutable'):
                modified._mutable = True
            return modified
        
        modified_post = post_data.copy()
        
        if hasattr(modified_post, '_mutable'):
            if not modified_post._mutable:
                modified_post._mutable = True
        
        for key in list(modified_post.keys()):
            if key.endswith('-school') and not key.endswith('-school_name'):
                value = modified_post.get(key)
                if not value:
                    continue
                    
                prefix = 'temp_school_'
                if value.startswith(prefix):
                    if value in temp_school_id_map:
                        modified_post[key] = temp_school_id_map[value]
                    else:
                        school_name = value[len(prefix):].replace('_', ' ')
                        
                        school = School.objects.filter(name__iexact=school_name).first()
                        if not school:
                            school_name_no_space = value[len(prefix):]
                            school = School.objects.filter(name__iexact=school_name_no_space).first()
                        
                        if school:
                            modified_post[key] = str(school.id)
                        else:
                            modified_post[key] = ''
        
        if hasattr(modified_post, '_mutable'):
            modified_post._mutable = True
        
        return modified_post

    def _build_temp_debater_id_map(self, created_debaters):
        temp_id_map = {}
        
        for key, debater in created_debaters.items():
            if not debater:
                continue
            
            if key.startswith('tid_'):
                tournament_id = key[4:]
                temp_id = f"temp_tid_{tournament_id}"
                temp_id_map[temp_id] = str(debater.id)
        
        return temp_id_map

    def _replace_temp_debater_ids_in_post(self, post_data, temp_debater_id_map):
        if not temp_debater_id_map:
            if not hasattr(post_data, '_mutable') or not post_data._mutable:
                post_data = post_data.copy()
                if hasattr(post_data, '_mutable'):
                    post_data._mutable = True
            return post_data
        
        if not hasattr(post_data, '_mutable') or not post_data._mutable:
            modified_post = post_data.copy()
            if hasattr(modified_post, '_mutable'):
                modified_post._mutable = True
        else:
            modified_post = post_data
        
        replacements_made = 0
        replacements_failed = 0
        
        for key in list(modified_post.keys()):
            if key.endswith('-speaker') or key.endswith('-debater_one') or key.endswith('-debater_two'):
                value = modified_post.get(key)
                if not value:
                    continue
                    
                if value.startswith('temp_tid_'):
                    if value in temp_debater_id_map:
                        new_value = temp_debater_id_map[value]
                        modified_post[key] = new_value
                        replacements_made += 1
                    else:
                        modified_post[key] = ''
                        replacements_failed += 1
        
        if hasattr(modified_post, '_mutable'):
            modified_post._mutable = True
        
        return modified_post

    def _process_debaters(self, formset, created_schools):
        if not formset.is_valid():
            return {}
        
        created_debaters = {}
        debater_data_for_api = []
        
        for form_data in formset.cleaned_data:
            if not form_data:
                continue
            
            existing_debater = form_data.get('existing_debater')
            tournament_id = form_data.get('tournament_id')
            
            if existing_debater:
                if self.has_api_data() and tournament_id:
                    self.get_api_handler().link_tournament_debater(tournament_id, existing_debater)
                
                debater_key = f"{existing_debater.first_name}_{existing_debater.last_name}_{existing_debater.school_id}"
                created_debaters[debater_key] = existing_debater
                if tournament_id:
                    created_debaters[f"tid_{tournament_id}"] = existing_debater
                continue
            
            first_name = form_data.get('first_name')
            last_name = form_data.get('last_name')
            school = form_data.get('school')
            school_name = form_data.get('school_name')
            
            if not school and school_name and school_name in created_schools:
                school = created_schools[school_name]
            
            if not (first_name and last_name and school):
                continue
            
            if self.has_api_data():
                debater_data_for_api.append({
                    'first_name': first_name,
                    'last_name': last_name,
                    'school': school,
                    'tournament_id': tournament_id
                })
            else:
                debater, created = Debater.objects.get_or_create(
                    first_name=first_name,
                    last_name=last_name,
                    school=school,
                    defaults={'novice_status': Debater.UNKNOWN}
                )
                debater_key = f"{first_name}_{last_name}_{school.id}"
                created_debaters[debater_key] = debater
                if tournament_id:
                    created_debaters[f"tid_{tournament_id}"] = debater
        
        if debater_data_for_api and self.has_api_data():
            api_created = self.get_api_handler().create_debaters_from_data(debater_data_for_api)
            if api_created:
                for data in debater_data_for_api:
                    tid = data.get('tournament_id')
                    if tid:
                        debater_id = self.get_api_handler()._debater_id_map.get(str(tid))
                        if debater_id:
                            try:
                                debater = Debater.objects.get(id=debater_id)
                                debater_key = f"{debater.first_name}_{debater.last_name}_{debater.school_id}"
                                created_debaters[debater_key] = debater
                                created_debaters[f"tid_{tid}"] = debater
                            except Debater.DoesNotExist:
                                pass
        
        return created_debaters


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
            
            if not debater_one:
                tid_one = team_data.get("debater_one_tournament_id")
                if tid_one and self.has_api_data():
                    debater_id = self.get_api_handler()._debater_id_map.get(str(tid_one))
                    if debater_id:
                        try:
                            debater_one = Debater.objects.get(id=debater_id)
                        except Debater.DoesNotExist:
                            pass
            
            if not debater_two:
                tid_two = team_data.get("debater_two_tournament_id")
                if tid_two and self.has_api_data():
                    debater_id = self.get_api_handler()._debater_id_map.get(str(tid_two))
                    if debater_id:
                        try:
                            debater_two = Debater.objects.get(id=debater_id)
                        except Debater.DoesNotExist:
                            pass
            
            if not (debater_one and debater_two):
                continue
            
            if not debater_one.school or not debater_two.school:
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
                tid = speaker_data.get("tournament_id")
                if tid and self.has_api_data():
                    debater_id = self.get_api_handler()._debater_id_map.get(str(tid))
                    if debater_id:
                        try:
                            speaker = Debater.objects.get(id=debater_id)
                        except Debater.DoesNotExist:
                            pass
            
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


    def _reindex_debaters(self, teams_to_update, speakers_to_update, novices_to_update):
        debaters_to_reindex = set()
        
        for team in teams_to_update:
            if team:
                for d in team.debaters.all():
                    debaters_to_reindex.add(d)

        for d in speakers_to_update:
            if d:
                debaters_to_reindex.add(d)
        for d in novices_to_update:
            if d:
                debaters_to_reindex.add(d)

        if debaters_to_reindex:
            ui = connections['default'].get_unified_index()
            debater_index = ui.get_index(Debater)
            for debater in debaters_to_reindex:
                debater_index.update_object(debater)

    def _update_search_index(self, created_schools, created_debaters):
        ui = connections['default'].get_unified_index()
        
        if created_schools:
            try:
                school_index = ui.get_index(School)
            except NotHandled:
                school_index = None
            if school_index:
                for school in created_schools.values():
                    if school:
                        school_index.update_object(school)
        
        if created_debaters:
            try:
                debater_index = ui.get_index(Debater)
            except NotHandled:
                debater_index = None
            if debater_index:
                for debater in created_debaters.values():
                    if debater:
                        debater_index.update_object(debater)
    
    def _annotate_error_context(self, context, tab_key):
        if not tab_key:
            return
        label = self.tab_labels.get(tab_key, tab_key.replace("_", " ").title())
        context["error_tab"] = tab_key
        context["error_tab_name"] = label
        context["error_message"] = f"Please fix the errors in the {label} tab before continuing."


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
