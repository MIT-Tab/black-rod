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

    def _debug_log(self, *args, **kwargs):  # No-op placeholder to avoid attribute errors
        return None

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
        self._debug_log("POST start", {
            "has_api": has_api,
            "tournament": getattr(tournament, "id", None),
            "temp_tid_keys": [k for k in request.POST.keys() if "temp_tid_" in request.POST.get(k, "")],
            "temp_school_keys": [k for k in request.POST.keys() if "temp_school_" in request.POST.get(k, "")],
        })
        try:
            with transaction.atomic():
                return self._handle_post_request(request, tournament, has_api)
        except FormsetValidationError as exc:
            self._debug_log("FormsetValidationError", {"error_tab": exc.context.get("error_tab")})
            return render(request, self.template_name, exc.context)

    def _handle_post_request(self, request, tournament, has_api):
        
        formsets = {}
        created_schools = {}
        created_debaters = {}
        temp_school_id_map = {}
        temp_debater_id_map = {}
        modified_post = request.POST
        
        if has_api:
            compacted_post = self._compact_schools_post(request.POST)
            schools_post = compacted_post if compacted_post is not None else request.POST
            schools_formset = SchoolCreationFormset(schools_post, prefix='schools')
            if not schools_formset.is_valid():
                if self._schools_form_blank(request.POST):
                    # User didn't touch schools; treat as no-op and continue
                    self._debug_log("schools_form_blank_skip", {
                        "total_forms": schools_formset.total_form_count(),
                        "non_form_errors": schools_formset.non_form_errors(),
                    })
                    schools_formset = SchoolCreationFormset(initial=[], prefix='schools')
                    formsets = {'schools': schools_formset}
                    modified_post = request.POST
                else:
                    rebuilt_from_initial = self._rebuild_schools_with_initial_overrides(request.POST, tournament)
                    if rebuilt_from_initial is not None:
                        retry_from_initial = SchoolCreationFormset(rebuilt_from_initial, prefix='schools')
                        if retry_from_initial.is_valid():
                            self._debug_log("schools_form_rebuilt_overrides_ok", {
                                "count": retry_from_initial.total_form_count()
                            })
                            schools_formset = retry_from_initial
                            modified_post = rebuilt_from_initial
                        else:
                            self._debug_log("schools_form_rebuilt_overrides_failed", {"errors": retry_from_initial.errors})
                    patched_post = self._fill_missing_school_fields(request.POST, tournament)
                    if patched_post is not None:
                        retry_formset = SchoolCreationFormset(patched_post, prefix='schools')
                        if retry_formset.is_valid():
                            self._debug_log("schools_form_patched_with_initial", {
                                "count": retry_formset.total_form_count()
                            })
                            schools_formset = retry_formset
                            modified_post = patched_post
                        else:
                            self._debug_log("schools_form_patch_failed", {"errors": retry_formset.errors})
                    rebuilt_post = self._rebuild_schools_post_with_initial(request.POST)
                    if rebuilt_post:
                        schools_formset = SchoolCreationFormset(rebuilt_post, prefix='schools')
                        if schools_formset.is_valid():
                            self._debug_log("schools_form_rebuilt_with_initial", {
                                "count": schools_formset.total_form_count()
                            })
                        else:
                            self._debug_log("schools_form_rebuild_failed", {"errors": schools_formset.errors})
                if not schools_formset.is_valid():
                    shrunk_post = self._shrink_schools_to_named_rows(request.POST)
                    if shrunk_post is not None:
                        shrink_formset = SchoolCreationFormset(shrunk_post, prefix='schools')
                        if shrink_formset.is_valid():
                            self._debug_log("schools_form_shrunk_named_rows", {"count": shrink_formset.total_form_count()})
                            schools_formset = shrink_formset
                            modified_post = shrunk_post
                            schools_valid = True
                    sanitized_post = self._replace_temp_ids_in_post(request.POST, {})
                    sanitized_post = self._replace_temp_debater_ids_in_post(sanitized_post, {})
                    formsets = {'schools': schools_formset}
                    for tab_key, (formset_class, prefix) in self.formset_config.items():
                        if tab_key != 'schools':
                            formsets[tab_key] = formset_class(sanitized_post, prefix=prefix)
                    
                    context = self._build_context(tournament, formsets, has_api)
                    self._annotate_error_context(context, "schools")
                    self._debug_log("schools_form_invalid", {
                        "errors": schools_formset.errors,
                        "raw_post_keys": [k for k in request.POST.keys() if k.startswith('schools-')],
                        "management": {
                            "TOTAL_FORMS": request.POST.get("schools-TOTAL_FORMS"),
                            "INITIAL_FORMS": request.POST.get("schools-INITIAL_FORMS"),
                        },
                        "names": {k: v for k, v in request.POST.items() if k.startswith("schools-") and "-name" in k},
                        "initial_len": len(self._get_initial_data("schools", tournament) or []),
                    })
                    raise FormsetValidationError(context)
                else:
                    schools_valid = True
            
            created_schools = self._process_schools(schools_formset)
            temp_school_id_map = self._build_temp_school_id_map(created_schools)
            
            modified_post = self._replace_temp_ids_in_post(request.POST, temp_school_id_map)
            
            formsets = {'schools': schools_formset}
            debaters_compacted_post = self._compact_debaters_post(modified_post)
            debaters_post = debaters_compacted_post if debaters_compacted_post is not None else modified_post
            debaters_formset = DebaterCreationFormset(debaters_post, prefix='debaters')
            
            is_valid = debaters_formset.is_valid()
            
            if not is_valid:
                if self._debaters_form_blank(modified_post):
                    self._debug_log("debaters_form_blank_skip", {
                        "total_forms": debaters_formset.total_form_count(),
                    })
                    debaters_formset = DebaterCreationFormset(initial=[], prefix='debaters')
                    formsets['debaters'] = debaters_formset
                else:
                    shrunk_debaters_post = self._shrink_debaters_to_named_rows(modified_post)
                    if shrunk_debaters_post is not None:
                        shrunk_formset = DebaterCreationFormset(shrunk_debaters_post, prefix='debaters')
                        if shrunk_formset.is_valid():
                            self._debug_log("debaters_form_shrunk_named_rows", {
                                "count": shrunk_formset.total_form_count()
                            })
                            debaters_formset = shrunk_formset
                            modified_post = shrunk_debaters_post
                            is_valid = True
                        else:
                            self._debug_log("debaters_form_shrink_failed", {"errors": shrunk_formset.errors})
                if not is_valid:
                    for tab_key, (formset_class, prefix) in self.formset_config.items():
                        if tab_key not in ['schools', 'debaters']:
                            initial_data = self._get_initial_data(tab_key, tournament)
                            formsets[tab_key] = formset_class(initial=initial_data, prefix=prefix)
                    formsets['debaters'] = debaters_formset
                    
                    context = self._build_context(tournament, formsets, has_api)
                    self._annotate_error_context(context, "debaters")
                    self._debug_log("debaters_form_invalid", {
                        "errors": debaters_formset.errors,
                        "management": {
                            "TOTAL_FORMS": modified_post.get("debaters-TOTAL_FORMS") if hasattr(modified_post, "get") else None,
                            "INITIAL_FORMS": modified_post.get("debaters-INITIAL_FORMS") if hasattr(modified_post, "get") else None,
                        },
                        "names": {k: v for k, v in modified_post.items() if isinstance(k, str) and k.startswith("debaters-") and ("first_name" in k or "last_name" in k)},
                    })
                    raise FormsetValidationError(context)
            
            formsets['debaters'] = debaters_formset
            
            created_debaters = self._process_debaters(debaters_formset, created_schools)
            
            temp_debater_id_map = self._build_temp_debater_id_map(created_debaters)
            
            modified_post = self._replace_temp_debater_ids_in_post(modified_post, temp_debater_id_map)
            if temp_debater_id_map:
                self._debug_log("temp_debater_map", {
                    "map_size": len(temp_debater_id_map),
                    "keys": list(temp_debater_id_map.keys())[:5],
                })
        
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
            self._debug_log("formset_invalid", {
                "invalid_tabs": invalid_tabs,
            })
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
            if not form_data or form_data.get('DELETE'):
                continue
            
            school_name = form_data.get('name')
            if not school_name:
                continue
            
            server_name = form_data.get('server_name', school_name)
            existing_school = form_data.get('existing_school') or form_data.get('_existing_match')
            
            if existing_school:
                if self.has_api_data():
                    SchoolLookup.objects.update_or_create(
                        server_name=server_name,
                        defaults={'school': existing_school}
                    )
                    self.get_api_handler().link_tournament_school(server_name, existing_school)
                
                created_schools[school_name] = existing_school
                created_schools[server_name] = existing_school
                continue

            if form_data.get('_skip_creation'):
                continue

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
                if not value.startswith(prefix):
                    continue

                if value in temp_school_id_map:
                    modified_post[key] = temp_school_id_map[value]
                    continue

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
        if not hasattr(post_data, '_mutable') or not post_data._mutable:
            modified_post = post_data.copy()
            if hasattr(modified_post, '_mutable'):
                modified_post._mutable = True
        else:
            modified_post = post_data
        
        replacements_made = 0
        replacements_failed = 0
        handler_map = {}
        if self.has_api_data():
            try:
                handler_map = self.get_api_handler()._debater_id_map  # noqa: SLF001
            except Exception:
                handler_map = {}
        
        for key in list(modified_post.keys()):
            if key.endswith('-speaker') or key.endswith('-debater_one') or key.endswith('-debater_two'):
                value = modified_post.get(key)
                if not value:
                    continue
                    
                if value.startswith('temp_tid_'):
                    tid = value[len('temp_tid_'):]
                    if value in temp_debater_id_map:
                        new_value = temp_debater_id_map[value]
                        modified_post[key] = new_value
                        replacements_made += 1
                    elif tid and tid in handler_map:
                        modified_post[key] = str(handler_map[tid])
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
            if not form_data or form_data.get('DELETE'):
                continue
            
            tournament_id = form_data.get('tournament_id')
            existing_debater = form_data.get('existing_debater') or form_data.get('_existing_match')
            
            if existing_debater:
                if self.has_api_data() and tournament_id:
                    self.get_api_handler().link_tournament_debater(tournament_id, existing_debater)
                
                debater_key = f"{existing_debater.first_name}_{existing_debater.last_name}_{existing_debater.school_id}"
                created_debaters[debater_key] = existing_debater
                if tournament_id:
                    created_debaters[f"tid_{tournament_id}"] = existing_debater
                continue

            if form_data.get('_skip_creation'):
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

    def _schools_form_blank(self, post_data):
        """
        Detect whether the schools formset was effectively untouched (no names or existing selections).
        """
        for key, value in post_data.items():
            if not key.startswith("schools-"):
                continue
            if any(field in key for field in ["name", "existing_school", "short_name"]):
                if str(value).strip():
                    return False
        return True

    def _rebuild_schools_post_with_initial(self, post_data):
        """
        When API initial data exists but only management fields were posted,
        rebuild a POST-like dict that includes initial school names so validation passes.
        """
        initial_schools = self._get_initial_data("schools", self._get_tournament())
        if not initial_schools:
            return None

        # If the posted data already has multiple school names, skip rebuild.
        names_present = any(
            key.startswith("schools-") and key.endswith("-name") and str(val).strip()
            for key, val in post_data.items()
        )
        if names_present:
            return None

        from django.http import QueryDict

        rebuilt = QueryDict(mutable=True)
        # Copy all posted keys first
        for key, val in post_data.items():
            rebuilt[key] = val

        total_forms = len(initial_schools)
        rebuilt["schools-TOTAL_FORMS"] = str(total_forms)
        rebuilt["schools-INITIAL_FORMS"] = str(total_forms)
        rebuilt["schools-MIN_NUM_FORMS"] = "0"
        rebuilt["schools-MAX_NUM_FORMS"] = "500"

        for idx, school_data in enumerate(initial_schools):
            name = school_data.get("name", "")
            server_name = school_data.get("server_name", name)
            rebuilt[f"schools-{idx}-name"] = name
            rebuilt[f"schools-{idx}-server_name"] = server_name
            rebuilt[f"schools-{idx}-included_in_oty"] = "on"
            rebuilt.setdefault(f"schools-{idx}-short_name", "")
            rebuilt.setdefault(f"schools-{idx}-existing_school", "")

        return rebuilt

    def _fill_missing_school_fields(self, post_data, tournament):
        """
        Fill missing school fields from initial API data when some rows are blank.
        """
        initial_schools = self._get_initial_data("schools", tournament)
        if not initial_schools:
            return None

        from django.http import QueryDict
        patched = QueryDict(mutable=True)
        for key, val in post_data.items():
            patched[key] = val

        total_forms = int(post_data.get("schools-TOTAL_FORMS", len(initial_schools)))
        # Ensure totals match available initial data length if missing
        patched["schools-TOTAL_FORMS"] = str(total_forms)
        patched["schools-INITIAL_FORMS"] = post_data.get("schools-INITIAL_FORMS", str(total_forms))

        for idx in range(total_forms):
            if idx >= len(initial_schools):
                continue
            name_key = f"schools-{idx}-name"
            server_key = f"schools-{idx}-server_name"
            include_key = f"schools-{idx}-included_in_oty"
            existing_key = f"schools-{idx}-existing_school"
            short_key = f"schools-{idx}-short_name"

            if not patched.get(name_key, "").strip():
                patched[name_key] = initial_schools[idx].get("name", "")
            if not patched.get(server_key, "").strip():
                patched[server_key] = initial_schools[idx].get("server_name", initial_schools[idx].get("name", ""))
            if include_key not in patched:
                patched[include_key] = "on"
            patched.setdefault(existing_key, "")
            patched.setdefault(short_key, "")

        return patched

    def _compact_schools_post(self, post_data):
        """
        Remove empty schools rows and renumber posted data to match only non-empty entries.
        Returns a new QueryDict or None if no changes needed.
        """
        from django.http import QueryDict
        rows = {}
        for key, val in post_data.items():
            if not key.startswith("schools-"):
                continue
            parts = key.split("-")
            if len(parts) < 3:
                continue
            try:
                idx = int(parts[1])
            except (TypeError, ValueError):
                continue
            field = "-".join(parts[2:])
            rows.setdefault(idx, {})[field] = val

        if not rows:
            return None

        keep = []
        for idx, fields in sorted(rows.items()):
            if any(str(fields.get(f, "")).strip() for f in ["name", "existing_school", "short_name", "server_name"]):
                keep.append((idx, fields))

        if not keep:
            return None

        if len(keep) == len(rows):
            return None

        compacted = QueryDict(mutable=True)
        # Copy non-school fields verbatim
        for key, val in post_data.items():
            if not key.startswith("schools-"):
                compacted[key] = val

        # Rebuild management counts
        total = len(keep)
        compacted["schools-TOTAL_FORMS"] = str(total)
        compacted["schools-INITIAL_FORMS"] = str(total)
        compacted["schools-MIN_NUM_FORMS"] = post_data.get("schools-MIN_NUM_FORMS", "0")
        compacted["schools-MAX_NUM_FORMS"] = post_data.get("schools-MAX_NUM_FORMS", "500")

        for new_idx, (_, fields) in enumerate(keep):
            for field, val in fields.items():
                compacted[f"schools-{new_idx}-{field}"] = val

        self._debug_log("schools_post_compacted", {
            "original_total": post_data.get("schools-TOTAL_FORMS"),
            "compacted_total": total,
            "kept_rows": len(keep),
        })
        return compacted

    def _rebuild_schools_with_initial_overrides(self, post_data, tournament):
        """
        Build a fresh POST for schools using initial API data, but apply any posted overrides
        (e.g., linking to existing school on the first row).
        """
        initial_schools = self._get_initial_data("schools", tournament)
        if not initial_schools:
            return None

        from django.http import QueryDict
        rebuilt = QueryDict(mutable=True)

        # Copy non-school keys
        for key, val in post_data.items():
            if not key.startswith("schools-"):
                rebuilt[key] = val

        total_forms = len(initial_schools)
        rebuilt["schools-TOTAL_FORMS"] = str(total_forms)
        rebuilt["schools-INITIAL_FORMS"] = str(total_forms)
        rebuilt["schools-MIN_NUM_FORMS"] = post_data.get("schools-MIN_NUM_FORMS", "0")
        rebuilt["schools-MAX_NUM_FORMS"] = post_data.get("schools-MAX_NUM_FORMS", "500")

        for idx, school_data in enumerate(initial_schools):
            name = post_data.get(f"schools-{idx}-name", school_data.get("name", ""))
            server_name = post_data.get(f"schools-{idx}-server_name", school_data.get("server_name", name))
            existing = post_data.get(f"schools-{idx}-existing_school", "")
            short_name = post_data.get(f"schools-{idx}-short_name", "")
            included = post_data.get(f"schools-{idx}-included_in_oty", "on")

            rebuilt[f"schools-{idx}-name"] = name
            rebuilt[f"schools-{idx}-server_name"] = server_name
            rebuilt[f"schools-{idx}-existing_school"] = existing
            rebuilt[f"schools-{idx}-short_name"] = short_name
            rebuilt[f"schools-{idx}-included_in_oty"] = included

        return rebuilt

    def _shrink_schools_to_named_rows(self, post_data):
        """
        If multiple management rows exist but only some have names, shrink to the named rows.
        """
        from django.http import QueryDict
        named_rows = []
        for key, val in post_data.items():
            if key.startswith("schools-") and key.endswith("-name"):
                try:
                    idx = int(key.split("-")[1])
                except (TypeError, ValueError):
                    continue
                if str(val).strip():
                    named_rows.append((idx, val))

        if not named_rows:
            return None

        named_rows.sort(key=lambda x: x[0])
        compacted = QueryDict(mutable=True)

        # Copy non-school fields
        for key, val in post_data.items():
            if not key.startswith("schools-"):
                compacted[key] = val

        total = len(named_rows)
        compacted["schools-TOTAL_FORMS"] = str(total)
        compacted["schools-INITIAL_FORMS"] = str(total)
        compacted["schools-MIN_NUM_FORMS"] = post_data.get("schools-MIN_NUM_FORMS", "0")
        compacted["schools-MAX_NUM_FORMS"] = post_data.get("schools-MAX_NUM_FORMS", "500")

        for new_idx, (old_idx, name) in enumerate(named_rows):
            compacted[f"schools-{new_idx}-name"] = name
            compacted[f"schools-{new_idx}-server_name"] = post_data.get(f"schools-{old_idx}-server_name", name)
            compacted[f"schools-{new_idx}-existing_school"] = post_data.get(f"schools-{old_idx}-existing_school", "")
            compacted[f"schools-{new_idx}-short_name"] = post_data.get(f"schools-{old_idx}-short_name", "")
            compacted[f"schools-{new_idx}-included_in_oty"] = post_data.get(f"schools-{old_idx}-included_in_oty", "on")

        return compacted

    def _compact_debaters_post(self, post_data):
        """
        Remove empty debater rows and renumber posted data to match only non-empty entries.
        """
        from django.http import QueryDict
        rows = {}
        for key, val in post_data.items():
            if not str(key).startswith("debaters-"):
                continue
            parts = str(key).split("-")
            if len(parts) < 3:
                continue
            try:
                idx = int(parts[1])
            except (TypeError, ValueError):
                continue
            field = "-".join(parts[2:])
            rows.setdefault(idx, {})[field] = val

        if not rows:
            return None

        keep = []
        for idx, fields in sorted(rows.items()):
            if any(str(fields.get(f, "")).strip() for f in ["first_name", "last_name", "school", "existing_debater"]):
                keep.append((idx, fields))

        if not keep or len(keep) == len(rows):
            return None

        compacted = QueryDict(mutable=True)
        for key, val in post_data.items():
            if not str(key).startswith("debaters-"):
                compacted[key] = val

        total = len(keep)
        compacted["debaters-TOTAL_FORMS"] = str(total)
        compacted["debaters-INITIAL_FORMS"] = str(total)
        compacted["debaters-MIN_NUM_FORMS"] = post_data.get("debaters-MIN_NUM_FORMS", "0")
        compacted["debaters-MAX_NUM_FORMS"] = post_data.get("debaters-MAX_NUM_FORMS", "500")

        for new_idx, (_, fields) in enumerate(keep):
            for field, val in fields.items():
                compacted[f"debaters-{new_idx}-{field}"] = val

        self._debug_log("debaters_post_compacted", {
            "original_total": post_data.get("debaters-TOTAL_FORMS") if hasattr(post_data, "get") else None,
            "compacted_total": total,
            "kept_rows": len(keep),
        })
        return compacted

    def _shrink_debaters_to_named_rows(self, post_data):
        """
        If multiple management rows exist but only some have names or existing links, shrink to those rows.
        """
        from django.http import QueryDict
        named_rows = []
        for key, val in post_data.items():
            if not str(key).startswith("debaters-") or not str(key).endswith("-first_name"):
                continue
            try:
                idx = int(str(key).split("-")[1])
            except (TypeError, ValueError):
                continue
            if str(val).strip():
                named_rows.append(idx)

        # Also include rows with existing_debater even if names blank
        for key, val in post_data.items():
            if not str(key).startswith("debaters-") or not str(key).endswith("-existing_debater"):
                continue
            try:
                idx = int(str(key).split("-")[1])
            except (TypeError, ValueError):
                continue
            if str(val).strip():
                if idx not in named_rows:
                    named_rows.append(idx)

        if not named_rows:
            return None

        named_rows.sort()
        compacted = QueryDict(mutable=True)
        for key, val in post_data.items():
            if not str(key).startswith("debaters-"):
                compacted[key] = val

        total = len(named_rows)
        compacted["debaters-TOTAL_FORMS"] = str(total)
        compacted["debaters-INITIAL_FORMS"] = str(total)
        compacted["debaters-MIN_NUM_FORMS"] = post_data.get("debaters-MIN_NUM_FORMS", "0")
        compacted["debaters-MAX_NUM_FORMS"] = post_data.get("debaters-MAX_NUM_FORMS", "500")

        for new_idx, old_idx in enumerate(named_rows):
            for field in ["first_name", "last_name", "school", "existing_debater", "tournament_id", "school_name", "alias_group"]:
                compacted[f"debaters-{new_idx}-{field}"] = post_data.get(f"debaters-{old_idx}-{field}", "")

        return compacted

    def _debaters_form_blank(self, post_data):
        """
        Detect whether the debaters formset was effectively untouched.
        """
        for key, value in post_data.items():
            if not str(key).startswith("debaters-"):
                continue
            if any(field in str(key) for field in ["first_name", "last_name", "school", "existing_debater"]):
                if str(value).strip():
                    return False
        return True


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
