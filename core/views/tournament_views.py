from datetime import timedelta
import json
import requests
from urllib.parse import urlparse
from dal import autocomplete
from django.conf import settings
from django.contrib import messages
from django.db.models import Q
from django.http import QueryDict
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import TemplateView
from django_filters import ChoiceFilter, FilterSet
from django_tables2 import Column
from formtools.wizard.views import SessionWizardView
from haystack.query import SearchQuerySet

from core.forms import (
    DebaterReconciliationFormset,
    SchoolReconciliationFormset,
    TournamentCreateForm,
    TournamentDetailForm,
    TournamentForm,
    TournamentImportForm,
    TournamentSelectionForm,
)
from core.models.debater import Debater
from core.models.round import Round
from core.models.school import School
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
from core.utils.rounds import get_tab_card_data


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
    form_class = TournamentCreateForm
    template_name = "tournaments/create.html"

    def form_valid(self, form):
        api_url = form.cleaned_data.get('api_url')
        
        if api_url:
            from core.utils.api_data import APIDataHandler
            api_handler = APIDataHandler(self.request)
            api_handler.set_api_url(api_url)
            
            is_valid, error_message = api_handler.validate_api_connection()
            if not is_valid:
                from django.contrib import messages
                messages.error(self.request, f"API Error: {error_message}")
                form.add_error('api_url', f"API Error: {error_message}")
                return self.form_invalid(form)
        
        response = super().form_valid(form)
        tournament = self.object
        
        return redirect(f"{reverse_lazy('core:tournament_dataentry')}?tournament={tournament.id}")
    
        return super().form_valid(form)


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
