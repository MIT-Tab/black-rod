from dal import autocomplete
from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse_lazy
from django_filters import FilterSet
from django_tables2 import Column
from haystack.query import SearchQuerySet

from core.forms import DebaterForm
from core.models.debater import Debater
from core.models.results.team import TeamResult
from core.models.round import Round
from core.models.standings.toty import TOTY
from core.utils.generics import (
    CustomCreateView,
    CustomDeleteView,
    CustomDetailView,
    CustomListView,
    CustomTable,
    CustomUpdateView,
)
from core.utils.perms import has_perm
from core.utils.rounds import get_tab_card_data


class DebaterFilter(FilterSet):
    class Meta:
        model = Debater
        fields = {
            "id": ["exact"],
            "first_name": ["icontains"],
            "last_name": ["icontains"],
            "school": ["exact"],
            "school__name": ["icontains"],
            "status": ["exact"],
        }


class DebaterTable(CustomTable):
    id = Column(linkify=True)

    first_name = Column(linkify=True)
    last_name = Column(linkify=True)

    school_name = Column(verbose_name="School", accessor="school__name")

    class Meta:
        model = Debater
        fields = ("id", "first_name", "last_name", "school_name", "status")


class DebaterListView(CustomListView):
    public_view = True
    model = Debater
    table_class = DebaterTable
    template_name = "debaters/list.html"

    filterset_class = DebaterFilter

    buttons = [
        {
            "name": "Create",
            "href": reverse_lazy("core:debater_create"),
            "perm": "core.add_debater",
            "class": "btn-success",
        }
    ]


def num_distinct_tournaments(team):
    return len(list({result.tournament.id for result in team.team_results.all()}))


class DebaterDetailView(CustomDetailView):
    public_view = True
    model = Debater
    template_name = "debaters/detail.html"

    buttons = [
        {
            "name": "Delete",
            "href": "core:debater_delete",
            "perm": "core.remove_debater",
            "class": "btn-danger",
            "include_pk": True,
        },
        {
            "name": "Edit",
            "href": "core:debater_update",
            "perm": "core.change_debater",
            "class": "btn-info",
            "include_pk": True,
        },
    ]

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        tournaments = []

        tournaments = [
            result.tournament
            for result in TeamResult.objects.filter(team__debaters=self.object).all()
        ]
        tournaments += [
            result.tournament for result in self.object.speaker_results.all()
        ]

        if "all" in self.request.GET:
            for team in self.object.teams.all():
                tournaments += [round.tournament for round in team.govs.all()]
                tournaments += [round.tournament for round in team.opps.all()]

        tournaments = list(set(tournaments))

        seasons = [tournament.season for tournament in tournaments]
        seasons = list(set(seasons))

        seasons.sort(key=lambda season: season, reverse=True)
        current_season = settings.CURRENT_SEASON

        if not len(seasons) == 0:
            current_season = self.request.GET.get("season", seasons[0])

        if current_season == "":
            current_season = seasons[0]

        seasons = [season for season in settings.SEASONS if season[0] in seasons]

        seasons.sort(key=lambda season: season[0], reverse=True)

        context["seasons"] = seasons

        context["current_season"] = current_season

        tournaments = [
            tournament
            for tournament in tournaments
            if tournament.season == current_season
        ]

        tournaments.sort(key=lambda tournament: tournament.date)

        tournament_render = []

        for tournament in tournaments:
            to_add = {}
            to_add["tournament"] = tournament
            to_append = []

            to_append += [
                ("team", result)
                for result in TeamResult.objects.filter(team__debaters=self.object)
                .filter(tournament=tournament)
                .order_by("-type_of_place")
                .all()
            ]
            to_append += [
                ("speaker", result)
                for result in self.object.speaker_results.filter(tournament=tournament)
                .order_by("-type_of_place")
                .all()
            ]

            team_result = (
                TeamResult.objects.filter(team__debaters=self.object)
                .filter(tournament=tournament)
                .first()
            )

            gov_round = Round.objects.filter(gov__debaters=self.object).filter(
                tournament=tournament
            )

            opp_round = Round.objects.filter(opp__debaters=self.object).filter(
                tournament=tournament
            )

            # THIS IS WHERE YOU HAVE TO CHANGE THINGS #
            team = None if not team_result else team_result.team

            if not team and (gov_round.exists() or opp_round.exists()):
                if gov_round.exists():
                    team = gov_round.first().gov
                else:
                    team = opp_round.first().opp

            to_add["team"] = team
            to_add["data"] = to_append
            to_add["tab_card"] = get_tab_card_data(team, tournament)

            tournament_render.append(to_add)

        context["results"] = tournament_render

        context["totys"] = TOTY.objects.filter(team__debaters=self.object).order_by(
            "place", "season"
        )

        context["sotys"] = self.object.soty.order_by("place", "season")

        context["notys"] = self.object.noty.order_by("place", "season")

        teams = list(self.object.teams.all())
        teams.sort(
            key=lambda team: (num_distinct_tournaments(team), team.toty_points),
            reverse=True,
        )

        context["teams"] = teams

        context["videos"] = []
        context["videos"] += list(self.object.pm_videos.all())
        context["videos"] += list(self.object.lo_videos.all())
        context["videos"] += list(self.object.mg_videos.all())
        context["videos"] += list(self.object.mo_videos.all())

        context["videos"] = [
            video for video in context["videos"] if has_perm(self.request.user, video)
        ]

        return context


class DebaterUpdateView(CustomUpdateView):
    model = Debater

    form_class = DebaterForm
    template_name = "debaters/update.html"

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        context["totys"] = TOTY.objects.filter(team__debaters=self.object).order_by(
            "place", "season"
        )

        context["sotys"] = self.object.soty.order_by("place", "season")

        context["notys"] = self.object.noty.order_by("place", "season")

        teams = list(self.object.teams.all())
        teams.sort(
            key=lambda team: (num_distinct_tournaments(team), team.toty_points),
            reverse=True,
        )

        context["teams"] = teams

        return context


class DebaterCreateView(CustomCreateView):
    model = Debater

    form_class = DebaterForm
    template_name = "debaters/create.html"

    def post(self, *args, **kwargs):
        to_return = super().post(*args, **kwargs)

        if "ajax" in self.request.POST:
            return HttpResponse(self.object.id)
        return to_return


class DebaterDeleteView(CustomDeleteView):
    model = Debater
    success_url = reverse_lazy("core:debater_list")

    template_name = "debaters/delete.html"


class DebaterAutocomplete(autocomplete.Select2QuerySetView):
    def get_result_label(self, record):
        return f"<{record.id}> {record.name} ({record.school.name})"

    def get_queryset(self):
        qs = None
        if not self.q:
            qs = Debater.objects
        if self.q:
            qs = SearchQuerySet().models(Debater).filter(content=self.q)

            qs = [q.pk for q in qs.all()]

            qs = Debater.objects.filter(id__in=qs)

        qs = qs.order_by("-pk")

        school = self.forwarded.get("school", None)

        if school:
            qs = qs.filter(school__id=school)

        return qs
