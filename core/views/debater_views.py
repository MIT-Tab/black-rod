from dal import autocomplete
from django import forms
from django.conf import settings
from django.http import HttpResponse
from django.db.models import Q
from django.urls import reverse_lazy
from django_filters import FilterSet, CharFilter
from django_tables2 import Column
from haystack.query import SearchQuerySet

from core.forms import DebaterForm
from core.models.debater import Debater
from core.models.debater_alias_group import DebaterAliasGroup
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


class SeasonFilterWidget(forms.MultiWidget):
    """Custom widget that renders two dropdowns: filter type and season"""

    def __init__(self, attrs=None):
        filter_type_choices = [
            ('', 'Select Filter Type'),
            ('first_season', 'Started in Season'),
            ('latest_season', 'Last Competed in Season'),
            ('competed_during', 'Competed During Season'),
        ]

        season_choices = [('', 'Select Season')] + list(settings.SEASONS)

        widgets = [
            forms.Select(choices=filter_type_choices, attrs={'class': 'form-control'}),
            forms.Select(choices=season_choices, attrs={'class': 'form-control'}),
        ]
        super().__init__(widgets, attrs)

    def decompress(self, value):
        if value:
            # Value format: "filter_type:season_value"
            parts = value.split(':', 1) if ':' in str(value) else ['', '']
            return parts
        return ['', '']

    def value_from_datadict(self, data, files, name):
        filter_type = data.get(f'{name}_0', '')
        season = data.get(f'{name}_1', '')
        if filter_type and season:
            return f'{filter_type}:{season}'
        return ''


class SeasonFilter(CharFilter):
    """Custom filter that handles the combined season filter logic"""

    def __init__(self, *args, **kwargs):
        kwargs['widget'] = SeasonFilterWidget()
        super().__init__(*args, **kwargs)

    def filter(self, qs, value):
        if not value or ':' not in value:
            return qs

        filter_type, season_value = value.split(':', 1)

        if filter_type == 'first_season':
            return qs.filter(first_season=season_value)
        if filter_type == 'latest_season':
            return qs.filter(latest_season=season_value)
        if filter_type == 'competed_during':
            return qs.filter(
                first_season__lte=season_value,
                latest_season__gte=season_value
            ).exclude(
                first_season__isnull=True,
                latest_season__isnull=True
            )

        return qs


class DebaterFilter(FilterSet):
    season_filter = SeasonFilter(
        label="Season Filter",
        help_text="Select filter type and season"
    )

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

        also_debated_under = []
        if self.object.alias_group:
            also_debated_under = list(
                self.object.alias_group.debaters.exclude(pk=self.object.pk)
                .select_related("school")
                .order_by("school__name", "first_name", "last_name")
            )

        context["also_debated_under"] = also_debated_under

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
        school_name = record.school.name if record.school else "Unaffiliated"
        return f"<{record.id}> {record.name} ({school_name})"

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


class DebaterAliasGroupAutocomplete(autocomplete.Select2QuerySetView):
    def get_queryset(self):
        qs = DebaterAliasGroup.objects.prefetch_related("debaters").order_by("label", "id")

        if self.q:
            qs = qs.filter(
                Q(label__icontains=self.q)
            )

        return qs

    def get_result_label(self, item):
        alias_count = item.debaters.count()
        if alias_count:
            return f"{item.name} ({alias_count} linked debater{'s' if alias_count != 1 else ''})"
        return item.name


class DinoAggregatedDebater:
    """
    Represents a single dino entry that may aggregate multiple debater profiles
    through alias groups.
    """
    def __init__(self, debater, opt_in_type):
        self.debater = debater
        self.opt_in_type = opt_in_type  # 'judge' or 'to'
        self._process_alias_group()
    
    @property
    def school_name(self):
        """Property for table column access - returns the schools list"""
        return self.schools
    
    def _process_alias_group(self):
        """Process alias group to determine display values"""
        if not self.debater.alias_group:
            # No alias group, use debater directly
            self.id = self.debater.id
            self.first_name = self.debater.first_name
            self.last_name = self.debater.last_name
            self.schools = [(self.debater.school.id, self.debater.school.name)] if self.debater.school else []
            self.status = self.debater.get_status_display()
            return
        
        # Has alias group - need to aggregate
        alias_debaters = self.debater.alias_group.debaters.filter(status=Debater.DINO)
        
        # Get schools (excluding "Unaffiliated") with IDs for linking
        schools_dict = {}
        unaffiliated_schools = {}
        for d in alias_debaters:
            if d.school:
                if d.school.name.lower() != "unaffiliated":
                    schools_dict[d.school.id] = d.school.name
                else:
                    unaffiliated_schools[d.school.id] = d.school.name
        
        # If no affiliated schools, include unaffiliated
        if not schools_dict and unaffiliated_schools:
            schools_dict = unaffiliated_schools
        
        # Determine which profile to link based on opt-in type
        if self.opt_in_type == 'judge':
            opted_in = alias_debaters.filter(dino_judge_contact_opt_in=True)
        else:  # 'to'
            opted_in = alias_debaters.filter(dino_to_contact_opt_in=True)
        
        # Pick the profile to link
        if opted_in.exists():
            # Check for unaffiliated profile among opted-in
            unaffiliated = opted_in.filter(school__name__iexact="unaffiliated").first()
            link_debater = unaffiliated if unaffiliated else opted_in.first()
        else:
            # Fallback to any dino in the group
            link_debater = alias_debaters.first()
        
        # Set display values
        self.id = link_debater.id
        self.first_name = link_debater.first_name
        self.last_name = link_debater.last_name
        # Store schools as list of (id, name) tuples sorted by name
        self.schools = sorted(schools_dict.items(), key=lambda x: x[1])
        self.status = "Dino"


class DinoTable(CustomTable):
    """Custom table for displaying aggregated dino entries"""
    id = Column(verbose_name="ID")
    first_name = Column(verbose_name="First Name")
    last_name = Column(verbose_name="Last Name")
    school_name = Column(verbose_name="School", orderable=False)
    
    class Meta:
        # Don't specify model since we're using custom objects
        fields = ("id", "first_name", "last_name", "school_name")
        attrs = {"class": "table table-striped"}
    
    def render_id(self, record):
        from django.utils.html import format_html
        return format_html('<a href="/core/debaters/{}">{}</a>', record.id, record.id)
    
    def render_first_name(self, record):
        from django.utils.html import format_html
        return format_html('<a href="/core/debaters/{}">{}</a>', record.id, record.first_name)
    
    def render_last_name(self, record):
        from django.utils.html import format_html
        return format_html('<a href="/core/debaters/{}">{}</a>', record.id, record.last_name)
    
    def render_school_name(self, record):
        from django.utils.html import format_html
        from django.utils.safestring import mark_safe
        
        if not record.schools:
            return ""
        
        # Create links for each school
        school_links = [
            format_html('<a href="/core/schools/{}">{}</a>', school_id, school_name)
            for school_id, school_name in record.schools
        ]
        
        # Join with commas
        return mark_safe(", ".join(str(link) for link in school_links))


class DinoJudgeListView(CustomListView):
    """List of graduated debaters open to judging opportunities"""
    public_view = True
    model = Debater
    table_class = DinoTable
    template_name = "debaters/judge_list.html"
    filterset_class = DebaterFilter

    def get_queryset(self):
        # Get all dinos with judge opt-in, apply filters first
        qs = Debater.objects.filter(
            status=Debater.DINO,
            dino_judge_contact_opt_in=True
        ).select_related('alias_group', 'school')
        
        # Apply filters from filterset
        if hasattr(self, 'filterset') and self.filterset is not None:
            qs = self.filterset.qs
        
        # Track which alias groups we've already included
        seen_alias_groups = set()
        aggregated_dinos = []
        
        for debater in qs.order_by('last_name', 'first_name'):
            if debater.alias_group:
                # Skip if we've already processed this alias group
                if debater.alias_group.id in seen_alias_groups:
                    continue
                seen_alias_groups.add(debater.alias_group.id)
            
            aggregated_dinos.append(DinoAggregatedDebater(debater, 'judge'))
        
        return aggregated_dinos
    
    def get_table_data(self):
        return self.get_queryset()
    
    def get_filterset(self, filterset_class):
        """Get the filterset instance, filtering on the base Debater queryset"""
        kwargs = self.get_filterset_kwargs(filterset_class)
        # Override the queryset to be the base Debater queryset
        kwargs['queryset'] = Debater.objects.filter(
            status=Debater.DINO,
            dino_judge_contact_opt_in=True
        )
        return filterset_class(**kwargs)


class DinoTOListView(CustomListView):
    """List of graduated debaters open to tournament observer opportunities"""
    public_view = True
    model = Debater
    table_class = DinoTable
    template_name = "debaters/to_list.html"
    filterset_class = DebaterFilter

    def get_queryset(self):
        # Get all dinos with TO opt-in, apply filters first
        qs = Debater.objects.filter(
            status=Debater.DINO,
            dino_to_contact_opt_in=True
        ).select_related('alias_group', 'school')
        
        # Apply filters from filterset
        if hasattr(self, 'filterset') and self.filterset is not None:
            qs = self.filterset.qs
        
        # Track which alias groups we've already included
        seen_alias_groups = set()
        aggregated_dinos = []
        
        for debater in qs.order_by('last_name', 'first_name'):
            if debater.alias_group:
                # Skip if we've already processed this alias group
                if debater.alias_group.id in seen_alias_groups:
                    continue
                seen_alias_groups.add(debater.alias_group.id)
            
            aggregated_dinos.append(DinoAggregatedDebater(debater, 'to'))
        
        return aggregated_dinos
    
    def get_table_data(self):
        return self.get_queryset()
    
    def get_filterset(self, filterset_class):
        """Get the filterset instance, filtering on the base Debater queryset"""
        kwargs = self.get_filterset_kwargs(filterset_class)
        # Override the queryset to be the base Debater queryset
        kwargs['queryset'] = Debater.objects.filter(
            status=Debater.DINO,
            dino_to_contact_opt_in=True
        )
        return filterset_class(**kwargs)
