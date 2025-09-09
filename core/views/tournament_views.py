from datetime import timedelta

import requests
from dal import autocomplete
from django.conf import settings
from django.db.models import Q
from django.http import QueryDict
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import TemplateView
from django_filters import ChoiceFilter, FilterSet
from django_tables2 import Column
from formtools.wizard.views import SessionWizardView
from haystack.query import SearchQuerySet

from django.http import JsonResponse
from django.template.loader import render_to_string
from core.forms import (
    DebaterForm,
    DebaterCreationFormset,
    DebaterReconciliationFormset,
    NoviceSpeakerResultFormset,
    NoviceTeamResultFormset,
    SchoolCreationFormset,
    SchoolForm,
    SchoolReconciliationFormset,
    TournamentDetailForm,
    TournamentForm,
    TournamentImportForm,
    TournamentSelectionForm,
    UnplacedTeamResultFormset,
    VarsitySpeakerResultFormset,
    VarsityTeamResultFormset,
)
from core.models.debater import Debater
from core.models.results.speaker import SpeakerResult
from core.models.results.team import TeamResult
from core.models.round import Round
from core.models.school import School
from core.models.standings.coty import COTY
from core.models.standings.noty import NOTY
from core.models.standings.online_qual import OnlineQUAL
from core.models.standings.qual import QUAL
from core.models.standings.soty import SOTY
from core.models.standings.toty import TOTY
from core.models.team import Team
from core.models.tournament import Tournament
from core.utils.generics import (
    CustomCreateView,
    CustomDeleteView,
    CustomDetailView,
    CustomListView,
    CustomMixin,
    CustomTable,
    CustomUpdateView,
    SeasonColumn,
)
from core.utils.import_management import (
    CREATE,
    LINK,
    clean_keys,
    create_debaters,
    create_round_stats,
    create_rounds,
    create_schools,
    create_speaker_awards,
    create_team_awards,
    create_teams,
    get_dict,
    get_num_novice_debaters,
    get_num_teams,
    lookup_school,
)
from core.utils.rankings import (
    redo_rankings,
    update_noty,
    update_online_quals,
    update_qual_points,
    update_soty,
    update_toty,
)
from core.utils.rounds import get_tab_card_data
from core.utils.team import get_or_create_team_for_debaters


class TournamentFilter(FilterSet):
    def __init__(self, data=None, *args, **kwargs):
        if not data:
            data = QueryDict(f"season={settings.CURRENT_SEASON}")

        super().__init__(data, *args, **kwargs)

    # Custom season filter using dropdown choices from settings.SEASONS
    season = ChoiceFilter(
        choices=settings.SEASONS, empty_label="Any Season", label="Season"
    )

    class Meta:
        model = Tournament
        fields = {
            "id": ["exact"],
            "name": ["icontains"],
            "qual_type": ["exact"],
        }


class TournamentTable(CustomTable):
    id = Column(linkify=True)

    name = Column(linkify=True)

    season_display = SeasonColumn(
        verbose_name="Season", accessor="season", order_by="season"
    )

    class Meta:
        model = Tournament
        fields = (
            "id",
            "name",
            "date",
            "season_display",
            "num_teams",
            "num_novice_debaters",
        )


class TournamentListView(CustomListView):
    public_view = True
    model = Tournament
    table_class = TournamentTable
    template_name = "tournaments/list.html"

    filterset_class = TournamentFilter

    buttons = [
        {
            "name": "Create",
            "href": reverse_lazy("core:tournament_create"),
            "perm": "core.add_tournament",
            "class": "btn-success",
        },
        {
            "name": "Enter Results",
            "href": reverse_lazy("core:tournament_dataentry"),
            "perm": "core.change_tournament",
            "class": "btn-primary",
        },
        {
            "name": "Import Results",
            "href": reverse_lazy("core:tournament_import"),
            "perm": "core.change_tournament",
            "class": "btn-info",
        },
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
    template_name = "tournaments/detail.html"

    buttons = [
        {
            "name": "Delete",
            "href": "core:tournament_delete",
            "perm": "core.remove_tournament",
            "class": "btn-danger",
            "include_pk": True,
        },
        {
            "name": "Edit",
            "href": "core:tournament_update",
            "perm": "core.change_tournament",
            "class": "btn-info",
            "include_pk": True,
        },
    ]

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        obj = self.object

        context["varsity_team_results"] = obj.team_results.filter(
            type_of_place=Debater.VARSITY,
            place__gt=0
        ).order_by("place")

        context["novice_team_results"] = obj.team_results.filter(
            type_of_place=Debater.NOVICE,
            place__gt=0
        ).exclude(
            team__in=obj.team_results.filter(type_of_place=Debater.VARSITY, place__gt=0).values_list("team", flat=True)
        ).order_by("place")

        vspeakers = list(
            obj.speaker_results.filter(type_of_place=Debater.VARSITY).order_by("place")
        )

        vspeakerCount = len(vspeakers)
        for i in range(vspeakerCount):
            if vspeakers[i].tie:
                vspeakers[i].place -= 1
            if i < vspeakerCount - 1 and vspeakers[i + 1].tie:
                vspeakers[i].tie = True

        context["varsity_speaker_results"] = vspeakers

        nspeakers = list(
            obj.speaker_results.filter(
                type_of_place=Debater.NOVICE,
            ).order_by("place")
        )

        nspeakerCount = len(nspeakers)
        for i in range(nspeakerCount):
            if i < nspeakerCount - 1 and nspeakers[i + 1].tie:
                nspeakers[i].tie = True
            if nspeakers[i].tie:
                nspeakers[i].place -= 1

        context["novice_speaker_results"] = nspeakers

        context["novice_speaker_results"] = nspeakers

        context["tab_cards_available"] = Round.objects.filter(
            tournament=self.object
        ).exists()

        teams = (
            Team.objects.filter(
                Q(govs__tournament=self.object) | Q(opps__tournament=self.object)
            )
            .distinct()
            .all()
        )

        context["teams"] = [
            (team, get_tab_card_data(team, self.object)) for team in teams
        ]

        return context


class TournamentUpdateView(CustomUpdateView):
    model = Tournament

    form_class = TournamentForm
    template_name = "tournaments/update.html"


class TournamentCreateView(CustomCreateView):
    model = Tournament

    form_class = TournamentForm
    template_name = "tournaments/create.html"


class TournamentDeleteView(CustomDeleteView):
    model = Tournament
    success_url = reverse_lazy("core:tournament_list")

    template_name = "tournaments/delete.html"


class AllTournamentAutocomplete(autocomplete.Select2QuerySetView):
    def get_result_label(self, record):
        return f"<{record.id}> {record.name} ({record.get_season_display()})"

    def get_queryset(self):
        qs = Tournament.objects.all()

        if self.q:
            qs = qs.filter(name__icontains=self.q)

        return qs


class TournamentAutocomplete(autocomplete.Select2QuerySetView):
    def get_result_label(self, record):
        return f"<{record.id}> {record.name} ({record.get_season_display()})"

    def get_queryset(self):
        qs = Tournament.objects.all()

        ids = []

        for item in qs:
            if item.team_results.count() == 0 and item.speaker_results.count() == 0:
                ids += [item.id]

        qs = qs.filter(id__in=ids)

        if self.q:
            qs = qs.filter(name__icontains=self.q)

        return qs


class ScheduleView(TemplateView):
    template_name = "tournaments/schedule.html"

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        current_season = self.request.GET.get("season", settings.CURRENT_SEASON)
        seasons = settings.SEASONS

        context["current_season"] = current_season
        context["seasons"] = seasons

        tournaments = Tournament.objects.filter(season=current_season)

        season_display = f"{current_season}-{str(int(current_season)+1)[2:]}"

        context["season_display"] = season_display

        months = {}

        for tournament in tournaments:
            if tournament.date.month in months:
                months[tournament.date.month] += [tournament]
            else:
                months[tournament.date.month] = [tournament]

        to_return = []

        for month, tournaments in months.items():
            to_add = {
                "month": month,
                "display": tournaments[0].date.strftime("%B"),
                "year": tournaments[0].date.year,
            }

            weeks = []

            tournaments.sort(key=lambda tournament: tournament.date.day)

            current_week = {
                "date": tournaments[0].date.day,
                "one_more": (tournaments[0].date + timedelta(days=1)).day,
                "tournaments": [],
            }

            for tournament in tournaments:
                if not current_week["date"] == tournament.date.day:
                    current_week["tournaments"].sort(
                        key=lambda tournament: (
                            (
                                1
                                if tournament.qual_type == 1
                                or tournament.qual_type == 2
                                else 0
                            ),
                            tournament.qual_type,
                        )
                    )
                    weeks.append(current_week)
                    current_week = {}

                if "date" not in current_week:
                    current_week["date"] = tournament.date.day
                    current_week["one_more"] = (tournament.date + timedelta(days=1)).day
                    current_week["tournaments"] = []

                current_week["tournaments"].append(tournament)

            current_week["tournaments"].sort(
                key=lambda tournament: (
                    1 if tournament.qual_type == 1 or tournament.qual_type == 2 else 0,
                    tournament.qual_type,
                )
            )
            weeks.append(current_week)

            weeks.sort(key=lambda week: week["date"])
            to_add["weeks"] = weeks

            to_return += [to_add]

        to_return.sort(key=lambda weeks: (weeks["year"], weeks["month"]))
        context["tournaments"] = to_return

        return context


class TournamentImportWizardView(CustomMixin, SessionWizardView):
    permission_required = "core.change_tournament"

    form_list = [
        TournamentSelectionForm,
        TournamentImportForm,
        TournamentDetailForm,
        SchoolReconciliationFormset,
        DebaterReconciliationFormset,
    ]
    template_name = "tournaments/tournament_entry.html"

    def get_template_names(self):
        if self.steps.current == "3":
            return ["tournaments/school_reconciliation.html"]
        if self.steps.current == "4":
            return ["tournaments/debater_reconciliation.html"]

        return super().get_template_names()

    def get_form_initial(self, step):
        storage_data = None
        tournament = None

        initial = []

        tournament = None

        if step == "0" and "tournament" in self.request.GET:
            tournament = Tournament.objects.filter(
                id=int(self.request.GET.get("tournament"))
            ).first()

            if tournament:
                initial = {"tournament": tournament}

        elif step != "0":
            storage_data = self.storage.get_step_data("0")

            tournament = Tournament.objects.get(id=storage_data.get("0-tournament"))

        if step == "2":
            storage_data = self.storage.get_step_data("1")
            response = get_dict(storage_data.get("response"))

            initial = {
                "num_teams": get_num_teams(
                    response["teams"], int(response["num_rounds"])
                ),
                "num_novices": get_num_novice_debaters(
                    response["teams"], int(response["num_rounds"])
                ),
            }

        if step == "3":
            storage_data = self.storage.get_step_data("1")
            response = get_dict(storage_data.get("response"))

            initial = []

            for school in response["schools"]:
                to_add = {"id": school["id"], "server_name": school["name"]}

                school = lookup_school(school["name"].strip())

                if school:
                    to_add["school"] = school

                initial += [to_add]

        if step == "4":
            storage_data = self.storage.get_step_data("1")
            response = get_dict(storage_data.get("response"))

            storage_data = self.storage.get_step_data("3")
            schools = clean_keys(storage_data.get("schools"))

            initial = []

            for team in response["teams"]:
                for debater in team["debaters"]:
                    found_debater = (
                        SearchQuerySet().models(Debater).filter(content=debater["name"])
                    )

                    school = School.objects.filter(
                        id=schools[team["school_id"]]["school"]
                    ).first()

                    found_debater = [
                        result.object for result in found_debater.all() if result.object
                    ]
                    found_debater = [
                        obj
                        for obj in found_debater
                        if obj.school.id == schools[team["school_id"]]["school"]
                        or obj.school.id == schools[team["hybrid_school_id"]]["school"]
                    ]

                    found_debater = found_debater[0] if len(found_debater) > 0 else None

                    hybrid_name = ""

                    names = schools[team["school_id"]]["name"]

                    if schools[team["hybrid_school_id"]]["name"] != "":
                        hybrid_name = schools[team["hybrid_school_id"]]["name"]

                    initial += [
                        {
                            "id": debater["id"],
                            "school_id": str(team["school_id"]),
                            "server_name": debater["name"],
                            "server_school_name": names,
                            "status": 0 if debater["status"] else 1,
                            "server_hybrid_school_name": hybrid_name,
                            "school": found_debater.school if found_debater else school,
                            "debater": found_debater,
                        }
                    ]

        return initial

    def get_response(self, url):
        url = url + "/json"

        request = requests.get(url, timeout=30)

        return request.content

    def get_form_step_data(self, form):
        to_return = form.data.copy()

        if self.steps.current == "1":
            response = self.get_response(form.cleaned_data["url"])

            to_return["response"] = response

        if self.steps.current == "3":
            storage_data = self.storage.get_step_data("1")
            response = get_dict(storage_data.get("response"))

            school_actions = {-1: {"school": -1, "name": ""}}

            for data in form.cleaned_data:
                if "id" not in data:
                    continue

                school_actions[int(data["id"])] = {
                    "action": CREATE if not data["school"] else LINK,
                    "id": int(data["id"]),
                    "name": data["server_name"],
                    "school": data["school"].id if data["school"] else -1,
                }

            to_return["schools"] = school_actions

        if self.steps.current == "4":
            storage_data = self.storage.get_step_data("1")
            response = get_dict(storage_data.get("response"))

            debater_actions = {-1: {"debater": -1, "name": ""}}

            for data in form.cleaned_data:
                if "id" not in data:
                    continue

                debater_actions[int(data["id"])] = {
                    "action": CREATE if not data["debater"] else LINK,
                    "id": int(data["id"]),
                    "name": data["server_name"],
                    "debater": data["debater"].id if data["debater"] else -1,
                    "school": data["school"].id if data["school"] else -1,
                    "school_id": int(data["school_id"]),
                    "status": int(data["status"]),
                }

            to_return["debaters"] = debater_actions

        return to_return

    def done(self, form_list, form_dict):
        storage_data = self.storage.get_step_data("1")
        response = get_dict(storage_data.get("response"))

        storage_data = self.storage.get_step_data("3")
        schools = clean_keys(storage_data.get("schools"))

        storage_data = self.storage.get_step_data("4")
        debaters = clean_keys(storage_data.get("debaters"))

        school_actions = create_schools(schools)
        debater_actions = create_debaters(school_actions, debaters)
        team_actions = create_teams(debater_actions, response["teams"])

        storage_data = self.storage.get_step_data("0")
        tournament = Tournament.objects.get(id=int(storage_data.get("0-tournament")))

        storage_data = self.storage.get_step_data("2")
        tournament.num_teams = int(storage_data.get("2-num_teams"))
        tournament.num_novice_debaters = int(storage_data.get("2-num_novices"))
        tournament.save()

        round_actions = create_rounds(team_actions, tournament, response["rounds"])
        create_round_stats(
            debater_actions, round_actions, tournament, response["stats"]
        )

        create_speaker_awards(
            debater_actions, response["speaker_results"], Debater.VARSITY, tournament
        )

        create_speaker_awards(
            debater_actions,
            response["novice_speaker_results"],
            Debater.NOVICE,
            tournament,
        )

        create_team_awards(
            team_actions, response["team_results"], Debater.VARSITY, tournament
        )

        create_team_awards(
            team_actions, response["novice_team_results"], Debater.NOVICE, tournament
        )

        return redirect(tournament.get_absolute_url())


class TournamentDataEntryWizardView(CustomMixin, SessionWizardView):
    permission_required = "core.change_tournament"

    step_names = {
        "0": "Tournament Selection",
        "1": "Tournament Update",
        "2": "Create New Schools",
        "3": "Create New Debaters",
        "4": "Varsity Team Awards",
        "5": "Varsity Speaker Awards",
        "6": "Novice Team Awards",
        "7": "Novice Speaker Awards",
        "8": "Non-placing Teams"
    }

    form_list = [
        TournamentSelectionForm,
        TournamentDetailForm,
        SchoolCreationFormset,
        DebaterCreationFormset,
        VarsityTeamResultFormset,
        VarsitySpeakerResultFormset,
        NoviceTeamResultFormset,
        NoviceSpeakerResultFormset,
        UnplacedTeamResultFormset
    ]
    template_name = "tournaments/data_entry.html"

    def get_template_names(self):
        if self.steps.current == "0":
            return ["tournaments/tournament_entry.html"]
        if self.steps.current == "1":
            return ["tournaments/tournament_entry.html"]
        if self.steps.current == "2":
            return ["tournaments/school_creation_entry.html"]
        if self.steps.current == "3":
            return ["tournaments/debater_creation_entry.html"]
        if self.steps.current == "4":
            return ["tournaments/team_result_entry.html"]
        if self.steps.current == "5":
            return ["tournaments/speaker_result_entry.html"]
        if self.steps.current == "6":
            return ["tournaments/team_result_entry.html"]
        if self.steps.current == "7":
            return ["tournaments/speaker_result_entry.html"]
        if self.steps.current == "8":
            return ["tournaments/unplaced_team_result_entry.html"]

        return super().get_template_names()

    def get_form_initial(self, step):
        storage_data = None
        tournament = None

        initial = []

        if not step == "0":
            storage_data = self.storage.get_step_data("0")
            tournament = Tournament.objects.get(
                id=int(storage_data.get("0-tournament"))
            )

        if step == "0" and "tournament" in self.request.GET:
            tournament = Tournament.objects.filter(
                id=int(self.request.GET.get("tournament"))
            ).first()

            if tournament:
                initial = {"tournament": tournament}

        if step == "1":
            initial = {
                "num_teams": tournament.num_teams,
                "num_novices": tournament.num_novice_debaters,
            }

        if step == "2":
            initial = []

        if step == "3":
            initial = []

        if step == "4":
            results = TeamResult.objects.filter(
            tournament=tournament, type_of_place=Debater.VARSITY, place__gt=0
            ).order_by("place")
            for result in results:
                initial += [
                    {
                    "debater_one": result.team.debaters.first(),
                    "debater_two": result.team.debaters.last(),
                    "ghost_points": result.ghost_points,
                    }
                ]

        if step == "5":
            results = SpeakerResult.objects.filter(
            tournament=tournament,
            type_of_place=Debater.VARSITY,
            place__gt=0
            ).order_by("place")
            for result in results:
                initial += [
                    {
                    "speaker": result.debater,
                    "tie": result.tie,
                    }
                ]

        if step == "6":
            results = TeamResult.objects.filter(
            tournament=tournament, type_of_place=Debater.NOVICE, place__gt=0
            ).order_by("place")
            for result in results:
                initial += [
                    {
                    "debater_one": result.team.debaters.first(),
                    "debater_two": result.team.debaters.last(),
                    }
                ]

        if step == "7":
            results = SpeakerResult.objects.filter(
            tournament=tournament, type_of_place=Debater.NOVICE, place__gt=0
            ).order_by("place")
            for result in results:
                initial += [
                    {
                    "speaker": result.debater,
                    "tie": result.tie,
                    }
                ]

        if step == "8":
            results = TeamResult.objects.filter(
                tournament=tournament, place=-1
            )

            for result in results:
                initial += [
                    {
                        "debater_one": result.team.debaters.first(),
                        "debater_two": result.team.debaters.last()
                    }
                ]

        return initial

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        context["title"] = self.step_names[self.steps.current]
        context["debater_form"] = DebaterForm()
        context["school_form"] = SchoolForm()
        return context

    def process_step(self, form):
        if self.steps.current == "2" and form.is_valid():
            for school_data in form.cleaned_data:
                if school_data and not school_data.get("DELETE", False):
                    if school_data.get('name'):
                        School.objects.get_or_create(
                            name=school_data["name"],
                            defaults={'included_in_oty': school_data.get("included_in_oty", True)}
                        )
        if self.steps.current == "3" and form.is_valid():
            for debater_data in form.cleaned_data:
                if debater_data and not debater_data.get("DELETE", False):
                    if all(debater_data.get(key) for key in ['first_name', 'last_name', 'school']):
                        Debater.objects.get_or_create(
                            first_name=debater_data["first_name"],
                            last_name=debater_data["last_name"],
                            school=debater_data["school"]
                        )
        return super().process_step(form)

    def done(self, form_list, form_dict):
        tournament = form_dict["0"].cleaned_data["tournament"]

        tournament.num_teams = form_dict["1"].cleaned_data["num_teams"]
        tournament.num_novice_debaters = form_dict["1"].cleaned_data["num_novices"]

        tournament.save()

        update_otys = settings.CURRENT_SEASON == tournament.season

        teams_to_update = []
        speakers_to_update = []
        novices_to_update = []

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

        for i in range(len(form_dict["4"].cleaned_data)):
            if (
                "debater_one" not in form_dict["4"].cleaned_data[i]
                or "debater_two" not in form_dict["4"].cleaned_data[i]
            ):
                continue

            if (
                not form_dict["4"].cleaned_data[i]["debater_one"]
                or not form_dict["4"].cleaned_data[i]["debater_two"]
            ):
                continue

            team = get_or_create_team_for_debaters(
                form_dict["4"].cleaned_data[i]["debater_one"],
                form_dict["4"].cleaned_data[i]["debater_two"],
            )

            if not team:
                continue

            place = form_dict["4"].cleaned_data[i].get("ORDER", i + 1)
            type_of_place = Debater.VARSITY
            ghost_points = form_dict["4"].cleaned_data[i]["ghost_points"]

            TeamResult.objects.create(
                tournament=tournament,
                team=team,
                type_of_place=type_of_place,
                place=place,
                ghost_points=ghost_points,
            )

            teams_to_update += [team]

        for i in range(len(form_dict["5"].cleaned_data)):
            if "speaker" not in form_dict["5"].cleaned_data[i]:
                continue

            place = form_dict["5"].cleaned_data[i].get("ORDER", i + 1)
            type_of_place = Debater.VARSITY
            speaker = form_dict["5"].cleaned_data[i]["speaker"]
            tie = form_dict["5"].cleaned_data[i]["tie"]

            if not speaker:
                continue

            SpeakerResult.objects.create(
                tournament=tournament,
                debater=speaker,
                type_of_place=type_of_place,
                place=place,
                tie=tie,
            )

            speakers_to_update += [speaker]

        for i in range(len(form_dict["7"].cleaned_data)):
            if (
                "debater_one" not in form_dict["7"].cleaned_data[i]
                or "debater_two" not in form_dict["7"].cleaned_data[i]
            ):
                continue

            if (
                not form_dict["7"].cleaned_data[i]["debater_one"]
                or not form_dict["7"].cleaned_data[i]["debater_two"]
            ):
                continue

            team = get_or_create_team_for_debaters(
                form_dict["7"].cleaned_data[i]["debater_one"],
                form_dict["7"].cleaned_data[i]["debater_two"],
            )

            if not team:
                continue

            place = form_dict["7"].cleaned_data[i].get("ORDER", i + 1)
            type_of_place = Debater.NOVICE

            TeamResult.objects.create(
                tournament=tournament,
                team=team,
                type_of_place=type_of_place,
                place=place,
            )

            teams_to_update += [team]

        for i in range(len(form_dict["6"].cleaned_data)):
            if "speaker" not in form_dict["6"].cleaned_data[i]:
                continue

            place = form_dict["6"].cleaned_data[i].get("ORDER", i + 1)
            type_of_place = Debater.NOVICE
            speaker = form_dict["6"].cleaned_data[i]["speaker"]
            tie = form_dict["6"].cleaned_data[i]["tie"]

            if not speaker:
                continue

            SpeakerResult.objects.create(
                tournament=tournament,
                debater=speaker,
                type_of_place=type_of_place,
                place=place,
                tie=tie,
            )

            novices_to_update += [speaker]

        for i in range(len(form_dict["8"].cleaned_data)):
            if (
                "debater_one" not in form_dict["8"].cleaned_data[i]
                or "debater_two" not in form_dict["8"].cleaned_data[i]
            ):
                continue

            if (
                not form_dict["8"].cleaned_data[i]["debater_one"]
                or not form_dict["8"].cleaned_data[i]["debater_two"]
            ):
                continue

            team = get_or_create_team_for_debaters(
                form_dict["8"].cleaned_data[i]["debater_one"],
                form_dict["8"].cleaned_data[i]["debater_two"],
            )

            if not team:
                continue

            place = -1
            type_of_place = Debater.VARSITY

            TeamResult.objects.create(
                tournament=tournament,
                team=team,
                type_of_place=type_of_place,
                place=place,
            )

            teams_to_update += [team]

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

            redo_rankings(
                TOTY.objects.filter(season=settings.CURRENT_SEASON),
                season=settings.CURRENT_SEASON,
                cache_type="toty",
            )
            redo_rankings(
                SOTY.objects.filter(season=settings.CURRENT_SEASON),
                season=settings.CURRENT_SEASON,
                cache_type="soty",
            )
            redo_rankings(
                NOTY.objects.filter(season=settings.CURRENT_SEASON),
                season=settings.CURRENT_SEASON,
                cache_type="noty",
            )
            redo_rankings(
                COTY.objects.filter(season=settings.CURRENT_SEASON),
                season=settings.CURRENT_SEASON,
                cache_type="coty",
            )
            redo_rankings(
                OnlineQUAL.objects.filter(season=settings.CURRENT_SEASON),
                season=settings.CURRENT_SEASON,
                cache_type="online_quals",
            )

        return redirect("core:tournament_detail", pk=tournament.id)


def get_new_team_form(request):
    """When we add a new team to the form, we make an ajax call here"""
    if request.method == 'GET':
        form_index = int(request.GET.get('form_index', 0))
        form_type = request.GET.get('form_type', 'team')
        
        if form_type == 'team':
            FormsetClass = VarsityTeamResultFormset
            step_prefix = '4'
        elif form_type == 'speaker':
            FormsetClass = VarsitySpeakerResultFormset
            step_prefix = '5'
        elif form_type == 'school':
            FormsetClass = SchoolCreationFormset
            step_prefix = '2'
        elif form_type == 'debater':
            FormsetClass = DebaterCreationFormset
            step_prefix = '3'
        else:
            FormsetClass = VarsitySpeakerResultFormset
            step_prefix = '5'
        
        temp_formset = FormsetClass()
        
        empty_form = temp_formset.empty_form
        empty_form.prefix = f'{step_prefix}-{form_index}'
        
        if hasattr(empty_form, 'fields') and 'ORDER' in empty_form.fields:
            empty_form.initial = {'ORDER': form_index + 1}
        
        html = render_to_string('tournaments/ajax_form_row.html', {
            'form': empty_form,
            'form_index': form_index,
            'place_number': form_index + 1,
            'form_type': form_type
        })
        
        return JsonResponse({'html': html})
    
    return JsonResponse({'error': 'Invalid request'}, status=400)
