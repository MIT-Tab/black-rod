import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.db import transaction
from django.db.models import Count
from django.forms import BaseModelFormSet, modelformset_factory
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views import View
from haystack import connections
from haystack.exceptions import NotHandled

from core.forms import (
    DebaterForm,
    DebaterImportFormsetBase,
    NoviceSpeakerResultFormset,
    NoviceTeamResultFormset,
    SchoolForm,
    SchoolImportFormsetBase,
    TournamentResultsImportOptionsForm,
    UnplacedTeamResultFormset,
    VarsitySpeakerResultFormset,
    VarsityTeamResultFormset,
)
from core.models.debater import Debater
from core.models.results.speaker import SpeakerResult
from core.models.results.team import TeamResult
from core.models.school import School, SchoolLookup
from core.models.standings.noty import NOTY
from core.models.standings.qual import QUAL
from core.models.standings.soty import SOTY
from core.models.standings.toty import TOTY
from core.models.tournament import Tournament
from core.utils.api_data import APIDataHandler
from core.utils.rankings import (
    rebuild_coty_related_rankings,
    redo_rankings,
    update_noty,
    update_soty,
    update_toty,
)
from core.utils.team import get_or_create_team_for_debaters


logger = logging.getLogger(__name__)
log_path = os.path.join(settings.BASE_DIR, "data_entry_debug.log")
if not logger.handlers:
    handler = logging.FileHandler(log_path)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

SchoolImportFormset = modelformset_factory(
    School,
    form=SchoolForm,
    extra=0,
    can_delete=True,
    max_num=500,
    formset=SchoolImportFormsetBase,
)
DebaterImportFormset = modelformset_factory(
    Debater,
    form=DebaterForm,
    extra=0,
    can_delete=True,
    max_num=500,
    formset=DebaterImportFormsetBase,
)

MAX_TEMP_DELETIONS = 500


@dataclass(frozen=True)
class ResultFormsetConfig:
    key: str
    formset_class: object
    type_of_place: int
    kind: str  # "team" or "speaker"
    has_ghost_points: bool = False
    place_value: int | None = None


@dataclass
class ImportResolution:
    schools: Dict[int, School] = field(default_factory=dict)
    debaters: Dict[int, Debater] = field(default_factory=dict)
    teams_to_update: List = field(default_factory=list)
    speakers_to_update: List = field(default_factory=list)
    novices_to_update: List = field(default_factory=list)


RESULT_FORMSET_CONFIGS: tuple[ResultFormsetConfig, ...] = (
    ResultFormsetConfig(
        "varsity_teams",
        VarsityTeamResultFormset,
        Debater.VARSITY,
        "team",
        has_ghost_points=True,
    ),
    ResultFormsetConfig(
        "novice_teams",
        NoviceTeamResultFormset,
        Debater.NOVICE,
        "team",
    ),
    ResultFormsetConfig(
        "unplaced_teams",
        UnplacedTeamResultFormset,
        Debater.VARSITY,
        "team",
        place_value=-1,
    ),
    ResultFormsetConfig(
        "varsity_speakers",
        VarsitySpeakerResultFormset,
        Debater.VARSITY,
        "speaker",
    ),
    ResultFormsetConfig(
        "novice_speakers",
        NoviceSpeakerResultFormset,
        Debater.NOVICE,
        "speaker",
    ),
)
RESULT_TAB_ORDER = [
    "schools",
    "debaters",
    "varsity_teams",
    "varsity_speakers",
    "novice_teams",
    "novice_speakers",
    "unplaced_teams",
]

API_IMPORTABLE_TABS = [
    "varsity_teams",
    "varsity_speakers",
    "novice_teams",
    "novice_speakers",
    "unplaced_teams",
]
API_TAB_TO_ENDPOINT = {
    "varsity_teams": ("team", "varsity-team-placements"),
    "novice_teams": ("team", "novice-team-placements"),
    "unplaced_teams": ("team", "non-placing-teams"),
    "varsity_speakers": ("speaker", "varsity-speaker-awards"),
    "novice_speakers": ("speaker", "novice-speaker-awards"),
}


def is_truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def cleanup_temporary_debaters(max_delete: int = MAX_TEMP_DELETIONS) -> int:
    qs = (
        Debater.all_objects.annotate(
            team_results_count=Count("teams__team_results", distinct=True),
            speaker_results_count=Count("speaker_results", distinct=True),
            team_count=Count("teams", distinct=True),
        )
        .filter(
            temporary=True,
            synthetic=False,
            team_results_count=0,
            speaker_results_count=0,
            team_count=0,
        )
    )
    count = qs.count()
    if count > max_delete:
        raise RuntimeError(
            f"Aborting cleanup: refusing to delete {count} temporary debaters."
        )
    qs.delete()
    return count


def cleanup_temporary_schools(max_delete: int = MAX_TEMP_DELETIONS) -> int:
    schools_with_debaters = set(
        Debater.all_objects.exclude(school=None).values_list("school_id", flat=True)
    )
    qs = School.all_objects.filter(temporary=True, synthetic=False).exclude(id__in=schools_with_debaters)
    count = qs.count()
    if count > max_delete:
        raise RuntimeError(
            f"Aborting cleanup: refusing to delete {count} temporary schools."
        )
    qs.delete()
    return count


def cleanup_temporary_entities(max_delete: int = MAX_TEMP_DELETIONS) -> None:
    cleanup_temporary_debaters(max_delete=max_delete)
    cleanup_temporary_schools(max_delete=max_delete)


def seed_temporary_schools(handler: APIDataHandler) -> Dict[str, School]:
    schools_by_server: Dict[str, School] = {}
    school_entries = handler.get_new_schools_from_api() or []
    for entry in school_entries:
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        server_name = entry.get("server_name") or name
        existing = School.all_objects.filter(name__iexact=name).first()
        if existing:
            schools_by_server[server_name] = existing
            handler.link_tournament_school(server_name, existing)
            continue
        school, _ = School.all_objects.get_or_create(
            name=name,
            defaults={
                "short_name": entry.get("short_name") or name,
                "included_in_oty": entry.get("included_in_oty", True),
                "temporary": True,
            },
        )
        schools_by_server[server_name] = school
        handler.link_tournament_school(server_name, school)
        SchoolLookup.objects.update_or_create(
            server_name=server_name, defaults={"school": school}
        )
    return schools_by_server


def seed_temporary_debaters(
    handler: APIDataHandler, schools_by_server: Dict[str, School]
) -> List[Debater]:
    debaters = []
    debater_entries = handler.get_new_debaters_from_api() or []
    for entry in debater_entries:
        first_name = (entry.get("first_name") or "").strip()
        last_name = (entry.get("last_name") or "").strip()
        tournament_id = entry.get("tournament_id")
        school_name = entry.get("school_name")
        school = entry.get("school") or (
            schools_by_server.get(school_name) if school_name else None
        )
        if not (first_name and last_name):
            continue
        existing = (
            Debater.all_objects.filter(
                first_name__iexact=first_name,
                last_name__iexact=last_name,
                school=school,
                synthetic=False,
            )
            .order_by("temporary", "id")
            .first()
        )
        if existing:
            if tournament_id:
                handler.link_tournament_debater(tournament_id, existing)
            continue
        debater = Debater.all_objects.create(
            first_name=first_name,
            last_name=last_name,
            school=school,
            status=Debater.NOVICE,
            temporary=True,
        )
        debaters.append(debater)
        if tournament_id:
            handler.link_tournament_debater(tournament_id, debater)
    return debaters


def build_team_initial(handler: APIDataHandler, endpoint: str) -> List[dict]:
    teams = handler.get_teams_from_api(endpoint) or []
    initial = []
    for idx, team in enumerate(teams):
        debater_one = team.get("debater_one")
        debater_two = team.get("debater_two")
        if not (debater_one and debater_two):
            continue
        initial.append(
            {
                "debater_one": debater_one,
                "debater_two": debater_two,
                "counts_for_points": True,
                "ORDER": idx + 1,
            }
        )
    return initial


def build_speaker_initial(handler: APIDataHandler, endpoint: str) -> List[dict]:
    speakers = handler.get_speakers_from_api(endpoint) or []
    initial = []
    for idx, speaker_data in enumerate(speakers):
        speaker = speaker_data.get("speaker")
        if not speaker:
            continue
        initial.append(
            {
                "speaker": speaker,
                "tie": speaker_data.get("tie", False),
                "counts_for_points": True,
                "ORDER": idx + 1,
            }
        )
    return initial


def build_api_initial(
    handler: APIDataHandler, selected_tabs: List[str] | set[str]
) -> Dict[str, List[dict]]:
    selected = set(selected_tabs)
    initial: Dict[str, List[dict]] = {}

    for tab_key, (kind, endpoint) in API_TAB_TO_ENDPOINT.items():
        if tab_key not in selected:
            continue
        if kind == "team":
            initial[tab_key] = build_team_initial(handler, endpoint)
        else:
            initial[tab_key] = build_speaker_initial(handler, endpoint)

    return initial


def get_db_initial(tab_key: str, tournament: Tournament) -> List[dict]:
    if tab_key in ["schools", "debaters"]:
        return []
    db_mapping = {
        "varsity_teams": (Debater.VARSITY, "team", {"place__gt": 0}),
        "varsity_speakers": (Debater.VARSITY, "speaker", {"place__gt": 0}),
        "novice_teams": (Debater.NOVICE, "team", {"place__gt": 0}),
        "novice_speakers": (Debater.NOVICE, "speaker", {"place__gt": 0}),
        "unplaced_teams": (Debater.VARSITY, "team", {"place": -1}),
    }
    if tab_key not in db_mapping:
        return []
    type_of_place, result_type, place_filter = db_mapping[tab_key]
    if result_type == "speaker":
        results = (
            SpeakerResult.objects.filter(
                tournament=tournament, type_of_place=type_of_place, **place_filter
            )
            .select_related("debater", "debater__school")
            .order_by("place")
        )
        return [
            {
                "speaker": r.debater,
                "tie": r.tie,
                "counts_for_points": r.counts_for_points,
            }
            for r in results
        ]
    results = (
        TeamResult.objects.filter(
            tournament=tournament, type_of_place=type_of_place, **place_filter
        )
        .select_related("team")
        .prefetch_related("team__debaters__school")
        .order_by("place")
    )
    initial = []
    for result in results:
        debaters = list(result.team.debaters.all())
        team_data = {
            "debater_one": debaters[0] if debaters else None,
            "debater_two": debaters[1] if len(debaters) > 1 else None,
            "counts_for_points": result.counts_for_points,
        }
        if type_of_place == Debater.VARSITY and result.place > 0:
            team_data["ghost_points"] = result.ghost_points
        initial.append(team_data)
    return initial


def create_team_results(
    tournament: Tournament,
    formset,
    type_of_place: int,
    teams_to_update: List,
    debater_resolution: Dict[int, Debater],
    has_ghost_points: bool = False,
    place_value: int | None = None,
) -> None:
    if not getattr(formset, "cleaned_data", None):
        return
    results_to_create = []
    for index, team_data in enumerate(formset.cleaned_data):
        if team_data.get("DELETE"):
            continue
        debater_one = team_data.get("debater_one")
        debater_two = team_data.get("debater_two")
        debater_one = debater_resolution.get(
            getattr(debater_one, "id", None), debater_one
        )
        debater_two = debater_resolution.get(
            getattr(debater_two, "id", None), debater_two
        )
        if not (debater_one and debater_two):
            continue
        if not debater_one.school or not debater_two.school:
            continue
        team = get_or_create_team_for_debaters(debater_one, debater_two)
        teams_to_update.append(team)
        final_place = place_value if place_value is not None else team_data.get(
            "ORDER", index + 1
        )
        result_data = {
            "tournament": tournament,
            "team": team,
            "type_of_place": type_of_place,
            "place": final_place,
            "counts_for_points": team_data.get("counts_for_points", True),
        }
        if has_ghost_points:
            result_data["ghost_points"] = team_data.get("ghost_points", False)
        results_to_create.append(TeamResult(**result_data))
    if results_to_create:
        TeamResult.objects.bulk_create(results_to_create)


def create_speaker_results(
    tournament: Tournament,
    formset,
    type_of_place: int,
    speakers_to_update: List,
    debater_resolution: Dict[int, Debater],
) -> None:
    if not getattr(formset, "cleaned_data", None):
        return
    results_to_create = []
    for index, speaker_data in enumerate(formset.cleaned_data):
        if speaker_data.get("DELETE"):
            continue
        speaker = speaker_data.get("speaker")
        speaker = debater_resolution.get(getattr(speaker, "id", None), speaker)
        if not speaker:
            continue
        results_to_create.append(
            SpeakerResult(
                tournament=tournament,
                debater=speaker,
                type_of_place=type_of_place,
                place=speaker_data.get("ORDER", index + 1),
                tie=speaker_data.get("tie", False),
                counts_for_points=speaker_data.get("counts_for_points", True),
            )
        )
        speakers_to_update.append(speaker)
    if results_to_create:
        SpeakerResult.objects.bulk_create(results_to_create)


def update_rankings(
    tournament: Tournament,
    teams_to_update: List,
    speakers_to_update: List,
    novices_to_update: List,
) -> None:
    if settings.CURRENT_SEASON != tournament.season:
        return
    teams_to_update = list(set(filter(None, teams_to_update)))
    speakers_to_update = list(set(filter(None, speakers_to_update)))
    novices_to_update = list(set(filter(None, novices_to_update)))
    if not (teams_to_update or speakers_to_update or novices_to_update):
        return

    for team in teams_to_update:
        update_toty(team)
    for debater in speakers_to_update:
        update_soty(debater)
    for debater in novices_to_update:
        update_noty(debater)

    rebuild_coty_related_rankings(season=tournament.season)

    rankings_to_update = [
        (TOTY, "toty"),
        (SOTY, "soty"),
        (NOTY, "noty"),
    ]
    for model, cache_type in rankings_to_update:
        redo_rankings(
            model.objects.filter(season=settings.CURRENT_SEASON),
            season=settings.CURRENT_SEASON,
            cache_type=cache_type,
        )


def reindex_debaters(
    teams_to_update: List, speakers_to_update: List, novices_to_update: List
) -> None:
    debaters_to_reindex = set()
    for team in teams_to_update:
        if team:
            for debater in team.debaters.all():
                debaters_to_reindex.add(debater)
    for debater in speakers_to_update:
        if debater:
            debaters_to_reindex.add(debater)
    for debater in novices_to_update:
        if debater:
            debaters_to_reindex.add(debater)
    if not debaters_to_reindex:
        return
    ui = connections["default"].get_unified_index()
    debater_index = ui.get_index(Debater)
    for debater in debaters_to_reindex:
        debater_index.update_object(debater)


class TournamentDataEntrySetupView(PermissionRequiredMixin, View):
    permission_required = "core.change_tournament"
    template_name = "tournaments/data_entry_setup.html"
    form_class = TournamentResultsImportOptionsForm

    def get_tournament(self) -> Tournament:
        tournament_id = self.request.GET.get("tournament") or self.request.POST.get(
            "tournament"
        )
        if not tournament_id:
            raise ValueError("Tournament ID must be provided as a URL parameter")
        return Tournament.objects.get(id=int(tournament_id))

    def get_initial(self) -> dict:
        params = self.request.GET
        initial = {"api_url": params.get("api_url", "")}
        for field_name in self.form_class.CATEGORY_FIELDS.values():
            if field_name in params:
                initial[field_name] = is_truthy(params.get(field_name))
        if "import_counts" in params:
            initial["import_counts"] = is_truthy(params.get("import_counts"))
        return initial

    def get(self, request, *args, **kwargs):
        tournament = self.get_tournament()
        form = self.form_class(initial=self.get_initial())
        return render(
            request,
            self.template_name,
            {"form": form, "tournament": tournament},
        )

    def post(self, request, *args, **kwargs):
        tournament = self.get_tournament()
        form = self.form_class(request.POST)

        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {"form": form, "tournament": tournament},
            )

        api_url = (form.cleaned_data.get("api_url") or "").strip()
        if api_url:
            api_handler = APIDataHandler(request)
            api_handler.set_api_url(api_url)
            is_valid, error_message = api_handler.validate_api_connection()
            if not is_valid:
                logger.warning(
                    "Mit-Tab tournament import connection validation failed",
                    extra={"error_detail": error_message},
                )
                form.add_error(
                    "api_url",
                    "Could not connect to the Mit-Tab tournament import source. "
                    "Check the URL and try again.",
                )
                return render(
                    request,
                    self.template_name,
                    {"form": form, "tournament": tournament},
                )

        params = {"tournament": tournament.id}
        if api_url:
            params["api_url"] = api_url

        for tab_key in form.selected_result_tabs():
            params[f"import_{tab_key}"] = "1"

        if form.cleaned_data.get("import_counts"):
            params["import_counts"] = "1"

        return redirect(f"{reverse('core:tournament_dataentry')}?{urlencode(params)}")


class TournamentDataEntryView(PermissionRequiredMixin, View):
    permission_required = "core.change_tournament"
    template_name = "tournaments/data_entry.html"
    result_formsets = RESULT_FORMSET_CONFIGS
    tab_order = RESULT_TAB_ORDER

    tab_labels = {
        "schools": "Schools",
        "debaters": "Debaters",
        "varsity_teams": "Varsity Teams",
        "varsity_speakers": "Varsity Speakers",
        "novice_teams": "Novice Teams",
        "novice_speakers": "Novice Speakers",
        "unplaced_teams": "Non-placing Teams",
    }

    def get_import_config(self) -> dict:
        if hasattr(self, "_import_config"):
            return self._import_config

        params = self.request.POST if self.request.method == "POST" else self.request.GET
        selected_tabs = {
            tab_key
            for tab_key in API_IMPORTABLE_TABS
            if is_truthy(params.get(f"import_{tab_key}"))
        }
        import_counts = is_truthy(params.get("import_counts"))
        api_url = (params.get("api_url") or "").strip()

        if not api_url:
            selected_tabs = set()
            import_counts = False

        self._import_config = {
            "api_url": api_url,
            "selected_tabs": selected_tabs,
            "import_counts": import_counts,
        }
        return self._import_config

    def get_api_handler(self) -> APIDataHandler:
        if hasattr(self, "_api_handler"):
            return self._api_handler

        handler = APIDataHandler(self.request)
        api_url = self.get_import_config().get("api_url")
        if api_url:
            handler.set_api_url(api_url)
        self._api_handler = handler
        return self._api_handler

    def use_api_results(self) -> bool:
        selected_tabs = self.get_import_config().get("selected_tabs", set())
        return bool(selected_tabs) and self.get_api_handler().should_use_api_data()

    def should_import_counts_from_api(self) -> bool:
        import_counts = self.get_import_config().get("import_counts", False)
        return bool(import_counts) and self.get_api_handler().should_use_api_data()

    def get_selected_api_tabs(self) -> List[str]:
        selected_tabs = self.get_import_config().get("selected_tabs", set())
        return [tab_key for tab_key in API_IMPORTABLE_TABS if tab_key in selected_tabs]

    @staticmethod
    def _parse_total_forms(data, prefix: str) -> int | None:
        raw_value = data.get(f"{prefix}-TOTAL_FORMS")
        if raw_value is None:
            return None
        try:
            return max(int(raw_value), 0)
        except (TypeError, ValueError):
            return None

    def get_tournament(self) -> Tournament:
        tournament_id = self.request.GET.get("tournament") or self.request.POST.get(
            "tournament"
        )
        if not tournament_id:
            raise ValueError("Tournament ID must be provided as a URL parameter")
        return Tournament.objects.get(id=int(tournament_id))

    def get(self, request, *args, **kwargs):
        tournament = self.get_tournament()
        use_api_results = self.use_api_results()
        selected_api_tabs = self.get_selected_api_tabs()
        api_state = (
            self.build_api_state(selected_api_tabs) if use_api_results else None
        )
        formsets = self.build_formsets(tournament, api_state, use_api_results)
        context = self.build_context(
            tournament,
            formsets,
            use_api_results,
            error_tab=None,
            current_api_url=self.get_api_handler().get_api_url(),
            selected_api_tabs=selected_api_tabs,
            import_counts_from_api=self.should_import_counts_from_api(),
        )
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        tournament = self.get_tournament()
        use_api_results = self.use_api_results()
        selected_api_tabs = self.get_selected_api_tabs()
        formsets = self.build_formsets(
            tournament, api_state=None, use_api=use_api_results, data=request.POST
        )

        if not self.forms_valid(formsets, use_api_results):
            self.log_formset_errors(formsets, request, use_api_results)
            context = self.build_context(
                tournament,
                formsets,
                use_api_results,
                error_tab=self.first_invalid_tab(formsets, use_api_results),
                current_api_url=self.get_api_handler().get_api_url(),
                selected_api_tabs=selected_api_tabs,
                import_counts_from_api=self.should_import_counts_from_api(),
            )
            return render(request, self.template_name, context)

        self.persist_results(
            tournament,
            formsets,
            use_api_results,
            import_counts_from_api=self.should_import_counts_from_api(),
        )
        return redirect("core:tournament_detail", pk=tournament.id)

    def build_api_state(self, selected_api_tabs: List[str]) -> dict:
        handler = self.get_api_handler()
        schools_by_server = seed_temporary_schools(handler)
        debaters = seed_temporary_debaters(handler, schools_by_server)
        return {
            "schools": list(schools_by_server.values()),
            "debaters": debaters,
            "initial": build_api_initial(handler, selected_api_tabs),
        }

    def build_formsets(
        self,
        tournament: Tournament,
        api_state: dict | None,
        use_api: bool,
        data=None,
    ):
        formsets: Dict[str, object] = {}
        team_kwargs = {"include_temporary_debaters": use_api}
        speaker_kwargs = {"include_temporary_debaters": use_api}
        self._show_school_tab = False
        self._show_debater_tab = False

        if use_api:
            school_ids = [s.id for s in api_state["schools"]] if api_state else []
            debater_ids = [d.id for d in api_state["debaters"]] if api_state else []
            if data is not None:
                school_qs = School.all_objects.all()
                debater_qs = Debater.all_objects.all()
                school_total_forms = self._parse_total_forms(data, "schools")
                debater_total_forms = self._parse_total_forms(data, "debaters")
                self._show_school_tab = (
                    school_total_forms is None or school_total_forms > 0
                )
                self._show_debater_tab = (
                    debater_total_forms is None or debater_total_forms > 0
                )
            else:
                school_qs = (
                    School.all_objects.filter(id__in=school_ids)
                    if school_ids
                    else School.all_objects.filter(temporary=True)
                )
                debater_qs = (
                    Debater.all_objects.filter(id__in=debater_ids)
                    if debater_ids
                    else Debater.all_objects.filter(temporary=True)
                )
                school_total_forms = school_qs.count()
                debater_total_forms = debater_qs.count()
                self._show_school_tab = school_total_forms > 0
                self._show_debater_tab = debater_total_forms > 0
            logger.info(
                "Building formsets",
                extra={
                    "mode": "POST" if data is not None else "GET",
                    "school_qs": school_qs.count(),
                    "debater_qs": debater_qs.count(),
                    "school_total_forms": school_total_forms,
                    "debater_total_forms": debater_total_forms,
                    "show_school_tab": self._show_school_tab,
                    "show_debater_tab": self._show_debater_tab,
                    "has_state": api_state is not None,
                    "school_ids": school_ids,
                    "debater_ids": debater_ids,
                },
            )
            formsets["schools"] = SchoolImportFormset(
                data=data,
                queryset=school_qs,
                prefix="schools",
                form_kwargs={"allow_blank_name": True},
            )
            formsets["debaters"] = DebaterImportFormset(
                data=data,
                queryset=debater_qs,
                prefix="debaters",
                form_kwargs={"include_temporary_schools": True},
            )

        initial = {}
        if data is None:
            initial = {
                config.key: get_db_initial(config.key, tournament)
                for config in self.result_formsets
            }
            if use_api and api_state:
                initial.update(api_state.get("initial", {}))

        for config in self.result_formsets:
            kwargs = {
                "prefix": config.key,
                "form_kwargs": team_kwargs if config.kind == "team" else speaker_kwargs,
            }
            if data is None:
                kwargs["initial"] = initial.get(config.key, [])
            else:
                kwargs["data"] = data
            formsets[config.key] = config.formset_class(**kwargs)
        return formsets

    def build_context(
        self,
        tournament: Tournament,
        formsets: Dict[str, object],
        has_api_data: bool,
        error_tab: str | None,
        current_api_url: str | None,
        selected_api_tabs: List[str],
        import_counts_from_api: bool,
    ):
        show_school_tab = bool(
            getattr(self, "_show_school_tab", False) and "schools" in formsets
        )
        show_debater_tab = bool(
            getattr(self, "_show_debater_tab", False) and "debaters" in formsets
        )
        visible_tabs = []
        for tab_key in self.tab_order:
            if tab_key not in formsets:
                continue
            if tab_key == "schools" and not show_school_tab:
                continue
            if tab_key == "debaters" and not show_debater_tab:
                continue
            visible_tabs.append(tab_key)

        active_tab = error_tab if error_tab in visible_tabs else None
        if not active_tab:
            active_tab = visible_tabs[0] if visible_tabs else "varsity_teams"

        return {
            "tournament": tournament,
            "formsets": formsets,
            "debater_form": DebaterForm(include_temporary_schools=has_api_data),
            "school_form": SchoolForm(),
            "has_api_data": has_api_data,
            "current_api_url": current_api_url,
            "error_tab": error_tab,
            "error_tab_name": self.tab_labels.get(error_tab),
            "error_message": f"Please fix the errors in the {self.tab_labels.get(error_tab, '')} tab before continuing."
            if error_tab
            else None,
            "show_school_tab": show_school_tab,
            "show_debater_tab": show_debater_tab,
            "active_tab": active_tab,
            "selected_api_tabs": selected_api_tabs,
            "import_counts_from_api": import_counts_from_api,
        }

    def log_formset_errors(
        self, formsets: Dict[str, object], request, use_api: bool
    ) -> None:
        error_summary = {}
        for key, formset in formsets.items():
            if formset.is_valid():
                continue
            error_summary[key] = {
                "non_form_errors": list(formset.non_form_errors()),
                "errors": [f.errors for f in formset.forms],
                "management_raw": dict(getattr(getattr(formset, "management_form", None), "data", {})),
                "management_clean": dict(getattr(getattr(formset, "management_form", None), "cleaned_data", {}))
                if hasattr(getattr(formset, "management_form", None), "cleaned_data")
                else {},
                "total_forms": formset.total_form_count(),
                "initial_forms": formset.initial_form_count(),
            }
        if not error_summary:
            return
        schools_sample = []
        if "schools" in error_summary:
            raw_data = getattr(formsets.get("schools"), "data", {}) or {}
            schools_sample = [
                (k, v) for k, v in raw_data.items() if str(k).startswith("schools-")
            ][:20]
        logger.warning(
            "Results import validation failed",
            extra={
                "api_mode": use_api,
                "request_keys": list(request.POST.keys()),
                "errors": error_summary,
                "schools_data_sample": schools_sample,
            },
        )

    def forms_valid(self, formsets: Dict[str, object], use_api: bool) -> bool:
        if use_api:
            for key in ["schools", "debaters"]:
                if key in formsets and not formsets[key].is_valid():
                    return False
        return all(formsets[cfg.key].is_valid() for cfg in self.result_formsets)

    def first_invalid_tab(self, formsets: Dict[str, object], use_api: bool) -> str | None:
        for key in self.tab_order:
            if key in ["schools", "debaters"] and not use_api:
                continue
            if key in formsets and not formsets[key].is_valid():
                return key
        return None

    def persist_results(
        self,
        tournament: Tournament,
        formsets: Dict[str, object],
        use_api: bool,
        import_counts_from_api: bool = False,
    ) -> None:
        with transaction.atomic():
            resolution = ImportResolution()
            if use_api:
                if "schools" in formsets:
                    resolution.schools = self.resolve_schools(formsets["schools"])
                if "debaters" in formsets:
                    resolution.debaters = self.resolve_debaters(
                        formsets["debaters"], resolution.schools
                    )

            if import_counts_from_api:
                self.update_tournament_counts_from_api(tournament)

            TeamResult.objects.filter(tournament=tournament).delete()
            SpeakerResult.objects.filter(tournament=tournament).delete()
            QUAL.objects.filter(tournament=tournament).delete()

            for config in self.result_formsets:
                formset = formsets[config.key]
                if config.kind == "team":
                    create_team_results(
                        tournament,
                        formset,
                        config.type_of_place,
                        resolution.teams_to_update,
                        resolution.debaters,
                        has_ghost_points=config.has_ghost_points,
                        place_value=config.place_value,
                    )
                    continue
                target_list = (
                    resolution.novices_to_update
                    if config.type_of_place == Debater.NOVICE
                    else resolution.speakers_to_update
                )
                create_speaker_results(
                    tournament,
                    formset,
                    config.type_of_place,
                    target_list,
                    resolution.debaters,
                )

            update_rankings(
                tournament,
                resolution.teams_to_update,
                resolution.speakers_to_update,
                resolution.novices_to_update,
            )
            reindex_debaters(
                resolution.teams_to_update,
                resolution.speakers_to_update,
                resolution.novices_to_update,
            )
            if use_api:
                cleanup_temporary_entities()

    def update_tournament_counts_from_api(self, tournament: Tournament) -> None:
        counts = self.get_api_handler().get_debater_counts_from_api() or {}
        updates = {}

        team_count = counts.get("teams")
        novice_count = counts.get("novice")

        try:
            if team_count is not None:
                parsed_team_count = int(team_count)
                if parsed_team_count < 0:
                    raise ValueError("teams count cannot be negative")
                updates["num_teams"] = parsed_team_count
        except (TypeError, ValueError):
            logger.warning("Invalid teams count from API: %s", team_count)

        try:
            if novice_count is not None:
                parsed_novice_count = int(novice_count)
                if parsed_novice_count < 0:
                    raise ValueError("novice count cannot be negative")
                updates["num_novice_debaters"] = parsed_novice_count
        except (TypeError, ValueError):
            logger.warning("Invalid novice count from API: %s", novice_count)

        if not updates:
            return

        for field_name, value in updates.items():
            setattr(tournament, field_name, value)
        tournament.save(update_fields=list(updates.keys()))

    def resolve_schools(self, formset) -> Dict[int, School]:
        resolution: Dict[int, School] = {}
        pending_deletes: List[School] = []
        for form in formset:
            cleaned = getattr(form, "cleaned_data", None) or {}
            if not cleaned:
                continue
            source_id = cleaned.get("id") or form.instance.pk
            delete_flag = cleaned.get("DELETE")
            existing_school = cleaned.get("existing_school")
            school_instance = form.instance

            if self._is_blank_school_row(cleaned, existing_school, delete_flag, source_id):
                continue

            if not source_id and not existing_school:
                existing_by_name = self._get_existing_school_by_name(cleaned)
                if existing_by_name:
                    source_id = existing_by_name.pk
                    school_instance = existing_by_name
                    form.instance = existing_by_name

            if delete_flag and getattr(school_instance, "temporary", False):
                pending_deletes.append(school_instance)
                continue

            if existing_school:
                if source_id:
                    resolution[source_id] = existing_school
                self.update_school_lookups(school_instance, existing_school)
                if getattr(school_instance, "temporary", False):
                    pending_deletes.append(school_instance)
                continue

            saved_school = form.save(commit=False)
            saved_school.short_name = saved_school.short_name or saved_school.name
            saved_school.temporary = False
            saved_school.save()
            resolved_key = source_id or saved_school.pk
            if resolved_key:
                resolution[resolved_key] = saved_school

        self._delete_orphan_schools(pending_deletes)
        return resolution

    def _is_blank_school_row(
        self,
        cleaned: dict,
        existing_school: School | None,
        delete_flag: bool,
        source_id: int | None,
    ) -> bool:
        if existing_school or delete_flag or source_id:
            return False
        name = (cleaned.get("name") or "").strip()
        server_name = (cleaned.get("server_name") or "").strip()
        return not (name or server_name)

    def resolve_debaters(
        self, formset, school_resolution: Dict[int, School]
    ) -> Dict[int, Debater]:
        resolution: Dict[int, Debater] = {}
        pending_deletes: List[Debater] = []
        for form in formset:
            cleaned = getattr(form, "cleaned_data", None) or {}
            if not cleaned:
                continue
            source_id = cleaned.get("id") or form.instance.pk
            delete_flag = cleaned.get("DELETE")
            existing_debater = cleaned.get("existing_debater")
            school = cleaned.get("school")
            if school and school.id in school_resolution:
                school = school_resolution[school.id]

            debater_instance = form.instance
            if not source_id and not existing_debater:
                match = self._find_existing_debater(cleaned, school)
                if match:
                    source_id = match.pk
                    debater_instance = match
                    form.instance = match

            if delete_flag and getattr(debater_instance, "temporary", False):
                pending_deletes.append(debater_instance)
                continue

            if existing_debater:
                if source_id:
                    resolution[source_id] = existing_debater
                if getattr(debater_instance, "temporary", False):
                    pending_deletes.append(debater_instance)
                continue

            saved_debater = form.save(commit=False)
            saved_debater.school = school
            saved_debater.temporary = False
            saved_debater.save()
            resolved_key = source_id or saved_debater.pk
            if resolved_key:
                resolution[resolved_key] = saved_debater

        self._delete_temporary_debaters(pending_deletes)
        return resolution

    def _get_existing_school_by_name(self, cleaned: dict) -> School | None:
        name = (cleaned.get("name") or "").strip()
        if not name:
            return None
        return School.all_objects.filter(name__iexact=name).first()

    def _find_existing_debater(
        self, cleaned: dict, school: School | None
    ) -> Debater | None:
        first_name = (cleaned.get("first_name") or "").strip()
        last_name = (cleaned.get("last_name") or "").strip()
        if not (first_name and last_name and school):
            return None
        return (
            Debater.all_objects.filter(
                first_name__iexact=first_name,
                last_name__iexact=last_name,
                school=school,
            )
            .order_by("-id")
            .first()
        )

    def _delete_orphan_schools(self, schools: List[School]) -> None:
        for school in schools:
            if not getattr(school, "temporary", False):
                continue
            has_debaters = Debater.all_objects.filter(school=school).exists()
            if not has_debaters:
                School.all_objects.filter(id=school.id).delete()

    def _delete_temporary_debaters(self, debaters: List[Debater]) -> None:
        for debater in debaters:
            if getattr(debater, "temporary", False):
                Debater.all_objects.filter(id=debater.id).delete()

    def update_school_lookups(self, source_school: School, target_school: School):
        lookups = SchoolLookup.objects.filter(school=source_school)
        for lookup in lookups:
            lookup.school = target_school
            lookup.save()


def get_new_team_form(request):
    if request.method != "GET":
        return JsonResponse({"error": "Invalid request"}, status=400)
    form_index = int(request.GET.get("form_index", 0))
    form_type = request.GET.get("form_type", "team")
    has_ghost_points = request.GET.get("has_ghost_points") in {"1", "true", "True"}
    item_name = request.GET.get("item_name") or (
        form_type.replace("_", " ").title() if form_type else "Item"
    )
    form_config = {
        "team": (VarsityTeamResultFormset, "2"),
        "speaker": (VarsitySpeakerResultFormset, "3"),
        "school": (SchoolImportFormset, "0"),
        "debater": (DebaterImportFormset, "1"),
    }
    FormsetClass, step_prefix = form_config.get(
        form_type, (VarsitySpeakerResultFormset, "3")
    )
    if issubclass(FormsetClass, BaseModelFormSet):
        if FormsetClass.model is School:
            formset_instance = FormsetClass(
                queryset=School.all_objects.none(),
                prefix="temp",
                form_kwargs={"allow_blank_name": True},
            )
        else:
            formset_instance = FormsetClass(
                queryset=Debater.all_objects.none(),
                prefix="temp",
                form_kwargs={"include_temporary_schools": True}
                if FormsetClass.model is Debater
                else {},
            )
    else:
        formset_instance = FormsetClass(
            form_kwargs={"include_temporary_debaters": True}
            if form_type in {"team", "speaker"}
            else {}
        )
    empty_form = formset_instance.empty_form
    empty_form.prefix = f"{step_prefix}-{form_index}"
    if "ORDER" in empty_form.fields:
        empty_form.initial = {"ORDER": form_index + 1}
    html = render_to_string(
        "tournaments/includes/formset_row.html",
        {
            "form": empty_form,
            "form_index": form_index,
            "place_number": form_index + 1,
            "form_type": form_type,
            "has_ghost_points": has_ghost_points,
            "item_name": item_name,
        },
    )
    return JsonResponse({"html": html})
