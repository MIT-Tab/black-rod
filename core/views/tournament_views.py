from datetime import timedelta
from urllib.parse import urlencode
from dal import autocomplete
from django.conf import settings
from django.contrib import messages
from django.db.models import Q, Prefetch
from django.core.cache import cache
from django.http import HttpResponse, QueryDict
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.generic import TemplateView
from django_filters import ChoiceFilter, FilterSet
from django_tables2 import Column
from core.forms import (
    TournamentCreateForm,
    TournamentForm,
)
from core.utils.api_data import APIDataHandler
from core.models.debater import Debater
from core.models.round import Round
from core.models.team import Team
from core.models.tournament import Tournament
from core.models.results.team import TeamResult
from core.models.results.speaker import SpeakerResult
from core.utils.generics import (
    CustomCreateView,
    CustomDeleteView,
    CustomDetailView,
    CustomListView,
    CustomTable,
    CustomUpdateView,
    SeasonColumn,
)
from core.utils.rounds import get_tab_card_data
from core.views.results_import_views import TournamentDataEntryView


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


class RecentResultsWidgetView(TemplateView):
    """
    Embeddable, frame-friendly widget that shows the past week's results.
    """

    template_name = "tournaments/recent_results_widget.html"
    public_view = True
    cache_timeout = 60 * 60 * 24  # 24 hours
    cache_key = "recent_results_widget_html"

    @method_decorator(xframe_options_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def get(self, request, *args, **kwargs):
        cached = cache.get(self.cache_key)
        if cached:
            return HttpResponse(cached, content_type="text/html; charset=utf-8")

        response = super().get(request, *args, **kwargs)
        response.render()
        cache.set(self.cache_key, response.content, self.cache_timeout)
        return response

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        today = timezone.now().date()
        start = today - timedelta(days=6)

        included_qual_types = [
            Tournament.POINTS,
            Tournament.GENDER_MINORITY,
            Tournament.BIPOC,
            Tournament.NATIONALS,
            Tournament.NOVICE,
            Tournament.PROAMS,
            Tournament.EXPANSION,
        ]

        def with_posted_finals(queryset):
            return (
                queryset.filter(team_results__type_of_place=Debater.VARSITY, team_results__place=1)
                .filter(team_results__type_of_place=Debater.VARSITY, team_results__place=2)
                .distinct()
            )

        base_qs = (
            Tournament.objects.filter(qual_type__in=included_qual_types, num_teams__gt=0)
            .exclude(qual_type=Tournament.ONLINE)
            .exclude(name__icontains="bp")
        )
        eligible_base_qs = with_posted_finals(base_qs)

        last_week_qs = eligible_base_qs.filter(date__range=(start, today))
        use_recent_week = last_week_qs.exists()

        if use_recent_week:
            selected_qs = last_week_qs.order_by("-date")
            scope_day = None
        else:
            latest_tournament = eligible_base_qs.order_by("-date").first()
            scope_day = latest_tournament.date if latest_tournament else None
            if scope_day:
                selected_qs = eligible_base_qs.filter(date=scope_day).order_by("name")
            else:
                selected_qs = eligible_base_qs.none()

        tournaments_qs = selected_qs.select_related("host").prefetch_related(
            Prefetch(
                "team_results",
                queryset=TeamResult.objects.filter(
                    type_of_place=Debater.VARSITY, place__in=[1, 2]
                )
                .select_related("team")
                .prefetch_related("team__debaters__school"),
                to_attr="widget_team_results",
            ),
            Prefetch(
                "speaker_results",
                queryset=SpeakerResult.objects.filter(
                    type_of_place=Debater.VARSITY, place__gt=0
                )
                .select_related("debater__school")
                .order_by("place"),
                to_attr="widget_speakers",
            ),
        )

        tournaments = []

        for tournament in tournaments_qs:
            winner = next(
                (res for res in tournament.widget_team_results if res.place == 1),
                None,
            )
            finalist = next(
                (res for res in tournament.widget_team_results if res.place == 2),
                None,
            )

            if not winner or not finalist:
                continue

            speakers = [
                {
                    "name": res.debater.name,
                    "school": res.debater.school.name if res.debater.school else "",
                    "place": res.place,
                    "tie": res.tie,
                }
                for res in tournament.widget_speakers[:3]
            ]

            tournaments.append(
                {
                    "name": tournament.display,
                    "host": tournament.host.name if tournament.host else "",
                    "date": tournament.date,
                    "num_teams": tournament.num_teams,
                    "winner": self._team_display(winner.team),
                    "finalist": self._team_display(finalist.team),
                    "speakers": speakers,
                    "url": tournament.get_absolute_url(),
                }
            )

            if use_recent_week and len(tournaments) >= 3:
                break

        # Final safety net: if strict filters yield nothing, show the latest day
        # that has any varsity winner/finalist data so the widget never renders empty.
        if not tournaments:
            fallback_all_qs = with_posted_finals(Tournament.objects.filter(num_teams__gt=0))
            fallback_latest = fallback_all_qs.order_by("-date").first()
            scope_day = fallback_latest.date if fallback_latest else None
            use_recent_week = False

            if scope_day:
                fallback_qs = fallback_all_qs.filter(date=scope_day).order_by("name").select_related(
                    "host"
                ).prefetch_related(
                    Prefetch(
                        "team_results",
                        queryset=TeamResult.objects.filter(
                            type_of_place=Debater.VARSITY, place__in=[1, 2]
                        )
                        .select_related("team")
                        .prefetch_related("team__debaters__school"),
                        to_attr="widget_team_results",
                    ),
                    Prefetch(
                        "speaker_results",
                        queryset=SpeakerResult.objects.filter(
                            type_of_place=Debater.VARSITY, place__gt=0
                        )
                        .select_related("debater__school")
                        .order_by("place"),
                        to_attr="widget_speakers",
                    ),
                )

                for tournament in fallback_qs:
                    winner = next(
                        (res for res in tournament.widget_team_results if res.place == 1),
                        None,
                    )
                    finalist = next(
                        (res for res in tournament.widget_team_results if res.place == 2),
                        None,
                    )
                    if not winner or not finalist:
                        continue

                    tournaments.append(
                        {
                            "name": tournament.display,
                            "host": tournament.host.name if tournament.host else "",
                            "date": tournament.date,
                            "num_teams": tournament.num_teams,
                            "winner": self._team_display(winner.team),
                            "finalist": self._team_display(finalist.team),
                            "speakers": [
                                {
                                    "name": res.debater.name,
                                    "school": res.debater.school.name if res.debater.school else "",
                                    "place": res.place,
                                    "tie": res.tie,
                                }
                                for res in tournament.widget_speakers[:3]
                            ],
                            "url": tournament.get_absolute_url(),
                        }
                    )

        context["tournaments"] = tournaments
        context["has_results"] = bool(tournaments)
        context["recent_window"] = (start, today)
        context["using_recent_week"] = use_recent_week
        context["scope_day"] = scope_day
        return context

    def _team_display(self, team):
        debaters = list(team.debaters.select_related("school").all())
        if not debaters:
            return {
                "debaters": team.name,
                "school": "",
                "url": team.get_absolute_url(),
            }

        if len(debaters) == 2:
            if debaters[0].school == debaters[1].school:
                school_name = debaters[0].school.name if debaters[0].school else "Unknown School"
            else:
                left = debaters[0].school.name if debaters[0].school else "Unknown School"
                right = debaters[1].school.name if debaters[1].school else "Unknown School"
                school_name = f"{left} / {right}"
            names = f"{debaters[0].name} and {debaters[1].name}"
            return {"debaters": names, "school": school_name, "url": team.get_absolute_url()}

        school_name = debaters[0].school.name if debaters[0].school else "Unknown School"
        return {"debaters": debaters[0].name, "school": school_name, "url": team.get_absolute_url()}


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

            api_handler = APIDataHandler(self.request)
            api_handler.set_api_url(api_url)

            is_valid, error_message = api_handler.validate_api_connection()
            if not is_valid:
                form.add_error('api_url', f"API Error: {error_message}")
                return self.form_invalid(form)

            super_response = super().form_valid(form)
            tournament = self.object

            # Redirect to the data entry view with tournament and API URL as query parameters
            params = urlencode({'tournament': tournament.id, 'api_url': api_url})
            return redirect(f"{reverse_lazy('core:tournament_dataentry')}?{params}")

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
