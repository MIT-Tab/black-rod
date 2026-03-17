from dal import autocomplete
from django import forms
from django.conf import settings
from django.db import connection
from django.db.models import Q
from django.http import HttpResponse
from django.urls import reverse_lazy
from django_filters import CharFilter, ChoiceFilter, FilterSet
from django_tables2 import Column
from haystack.query import SearchQuerySet

from core.access import can_download_debater_tab_cards
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
from core.utils.rounds import get_tab_card_data, visible_canonical_rounds
from core.views.debater_export_views import (
    build_debater_partner_breakdown,
)


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


class DebaterOutreachFilter(DebaterFilter):
    region = ChoiceFilter(
        label="Region",
        choices=Debater.REGION_CHOICES,
        empty_label="All regions",
        method="filter_region",
    )

    class Meta(DebaterFilter.Meta):
        fields = {**DebaterFilter.Meta.fields, "region": ["exact"]}

    def filter_region(self, queryset, name, value):
        if not value:
            return queryset
        if connection.features.supports_json_field_contains:
            return queryset.filter(region__contains=[value])
        evaluation_qs = queryset._chain()
        ids = [
            debater.pk
            for debater in evaluation_qs
            if value in getattr(debater, "region_list", [])
        ]
        filtered_qs = queryset._chain()
        if not ids:
            return filtered_qs.none()
        return filtered_qs.filter(pk__in=ids)


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

        tournaments = [
            result.tournament
            for result in TeamResult.objects.filter(team__debaters=self.object, team__synthetic=False)
        ]
        tournaments += [result.tournament for result in self.object.speaker_results.all()]

        if "all" in self.request.GET:
            for team in self.object.teams.filter(synthetic=False):
                tournaments += [
                    round_obj.tournament
                    for round_obj in visible_canonical_rounds(team.govs.all())
                ]
                tournaments += [
                    round_obj.tournament
                    for round_obj in visible_canonical_rounds(team.opps.all())
                ]

        tournaments = list(set(tournaments))
        seasons = list(set([tournament.season for tournament in tournaments]))
        seasons.sort(key=lambda season: season, reverse=True)

        current_season = settings.CURRENT_SEASON
        if seasons:
            current_season = self.request.GET.get("season", seasons[0]) or seasons[0]

        season_choices = [season for season in settings.SEASONS if season[0] in seasons]
        season_choices.sort(key=lambda season: season[0], reverse=True)
        context["seasons"] = season_choices
        context["current_season"] = current_season

        tournaments = [tournament for tournament in tournaments if tournament.season == current_season]
        tournaments.sort(key=lambda tournament: tournament.date)

        tournament_render = []
        for tournament in tournaments:
            team_results = list(
                TeamResult.objects.filter(team__debaters=self.object, team__synthetic=False)
                .filter(tournament=tournament)
                .order_by("-type_of_place")
            )
            speaker_results = list(
                self.object.speaker_results.filter(tournament=tournament).order_by("-type_of_place")
            )

            team_result = (
                TeamResult.objects.filter(team__debaters=self.object, team__synthetic=False)
                .filter(tournament=tournament)
                .first()
            )
            gov_round = visible_canonical_rounds(
                Round.objects.filter(gov__debaters=self.object, gov__synthetic=False).filter(
                    tournament=tournament
                )
            )
            opp_round = visible_canonical_rounds(
                Round.objects.filter(opp__debaters=self.object, opp__synthetic=False).filter(
                    tournament=tournament
                )
            )

            team = None if not team_result else team_result.team
            if not team and (gov_round.exists() or opp_round.exists()):
                team = gov_round.first().gov if gov_round.exists() else opp_round.first().opp

            tournament_render.append(
                {
                    "tournament": tournament,
                    "team": team,
                    "data": [("team", result) for result in team_results]
                    + [("speaker", result) for result in speaker_results],
                    "tab_card": get_tab_card_data(team, tournament),
                }
            )

        context["results"] = tournament_render
        context["totys"] = TOTY.objects.filter(team__debaters=self.object, team__synthetic=False).order_by(
            "place",
            "season",
        )
        context["sotys"] = self.object.soty.order_by("place", "season")
        context["notys"] = self.object.noty.order_by("place", "season")

        teams = list(self.object.teams.filter(synthetic=False))
        teams.sort(key=lambda team: (num_distinct_tournaments(team), team.toty_points), reverse=True)
        context["teams"] = teams

        also_debated_under = []
        if self.object.alias_group:
            also_debated_under = list(
                self.object.alias_group.debaters.exclude(pk=self.object.pk)
                .select_related("school")
                .order_by("school__name", "first_name", "last_name")
            )
        context["also_debated_under"] = also_debated_under

        videos = []
        videos += list(self.object.pm_videos.all())
        videos += list(self.object.lo_videos.all())
        videos += list(self.object.mg_videos.all())
        videos += list(self.object.mo_videos.all())
        context["videos"] = [video for video in videos if has_perm(self.request.user, video)]
        context["partner_breakdown"] = build_debater_partner_breakdown(self.object)
        context["can_download_tab_cards_csv"] = can_download_debater_tab_cards(
            self.request.user,
            self.object,
        )
        return context


class DebaterUpdateView(CustomUpdateView):
    model = Debater
    form_class = DebaterForm
    template_name = "debaters/update.html"

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        context["totys"] = TOTY.objects.filter(team__debaters=self.object, team__synthetic=False).order_by(
            "place",
            "season",
        )
        context["sotys"] = self.object.soty.order_by("place", "season")
        context["notys"] = self.object.noty.order_by("place", "season")
        teams = list(self.object.teams.filter(synthetic=False))
        teams.sort(key=lambda team: (num_distinct_tournaments(team), team.toty_points), reverse=True)
        context["teams"] = teams
        return context


class DebaterCreateView(CustomCreateView):
    model = Debater
    form_class = DebaterForm
    template_name = "debaters/create.html"

    def post(self, *args, **kwargs):
        response = super().post(*args, **kwargs)
        if "ajax" in self.request.POST:
            return HttpResponse(self.object.id)
        return response


class DebaterDeleteView(CustomDeleteView):
    model = Debater
    success_url = reverse_lazy("core:debater_list")

    template_name = "debaters/delete.html"


class DebaterAutocomplete(autocomplete.Select2QuerySetView):
    def get_result_label(self, record):
        school_name = record.school.name if record.school else "Unaffiliated"
        return f"<{record.id}> {record.display_name} ({school_name})"

    def get_queryset(self):
        base_manager = (
            Debater.all_objects if self.request.user.has_perm("core.change_tournament") else Debater.objects
        )
        if not self.q:
            qs = base_manager.all()
        else:
            search_ids = [row.pk for row in SearchQuerySet().models(Debater).filter(content=self.q).all()]
            qs = base_manager.filter(id__in=search_ids)

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
    
    @property
    def latest_year(self):
        """Property for table column access - returns the latest season string (e.g. '2024-25')"""
        return self.latest_season
    
    def _process_alias_group(self):
        """Process alias group to determine display values"""
        if not self.debater.alias_group:
            # No alias group, use debater directly
            self.id = self.debater.id
            self.first_name = self.debater.first_name
            self.last_name = self.debater.last_name
            self.schools = [(self.debater.school.id, self.debater.school.name)] if self.debater.school else []
            self.status = self.debater.get_status_display()
            self.latest_season = self.debater.latest_season
            self.latest_season_year = self._parse_season_year(self.debater.latest_season)
            if self.debater.show_region:
                self.region_value = self.debater.region_list
                self.region_display = self.debater.get_region_display()
            else:
                self.region_value = []
                self.region_display = ""
            return
        
        # Has alias group - need to aggregate
        # For judge database, only look at dinos; for TO database, look at all statuses
        if self.opt_in_type == 'judge':
            alias_debaters = self.debater.alias_group.debaters.filter(status=Debater.DINO)
        else:  # 'to'
            alias_debaters = self.debater.alias_group.debaters.all()
        
        # Get schools (excluding "Unaffiliated") with IDs for linking
        schools_dict = {}
        unaffiliated_schools = {}
        latest_season_affiliated = None
        latest_season_affiliated_year = None
        
        for d in alias_debaters:
            if d.school:
                if d.school.name.lower() != "unaffiliated":
                    schools_dict[d.school.id] = d.school.name
                    # Track latest season from affiliated schools
                    season_year = self._parse_season_year(d.latest_season)
                    if season_year and (latest_season_affiliated_year is None or season_year > latest_season_affiliated_year):
                        latest_season_affiliated_year = season_year
                        latest_season_affiliated = d.latest_season
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
            # Fallback to any in the group
            link_debater = alias_debaters.first()
        
        # Set display values
        self.id = link_debater.id
        self.first_name = link_debater.first_name
        self.last_name = link_debater.last_name
        # Store schools as list of (id, name) tuples sorted by name
        self.schools = sorted(schools_dict.items(), key=lambda x: x[1])
        self.status = link_debater.get_status_display()
        # Use the latest affiliated season, or fall back to link_debater's season
        self.latest_season = latest_season_affiliated if latest_season_affiliated is not None else link_debater.latest_season
        self.latest_season_year = latest_season_affiliated_year if latest_season_affiliated_year is not None else self._parse_season_year(link_debater.latest_season)
        if getattr(link_debater, "show_region", False):
            self.region_value = link_debater.region_list
            self.region_display = link_debater.get_region_display()
        else:
            self.region_value = []
            self.region_display = ""
    
    def _parse_season_year(self, season):
        """Parse season string to integer year, return None if invalid"""
        if not season:
            return None
        try:
            return int(str(season).split('-')[0])
        except (ValueError, IndexError):
            return None
    
    def _format_season_display(self, season):
        """Format season year to display format (e.g., '2024' -> '2024-25')"""
        if not season:
            return None
        try:
            year = int(str(season).split('-')[0])
            next_year = str(year + 1)[2:]
            return f"{year}-{next_year}"
        except (ValueError, IndexError):
            return season  # Return as-is if parsing fails


class DinoTable(CustomTable):
    """Custom table for displaying aggregated dino entries"""
    id = Column(verbose_name="ID")
    first_name = Column(verbose_name="First Name")
    last_name = Column(verbose_name="Last Name")
    school_name = Column(verbose_name="School", orderable=False)
    region = Column(verbose_name="Region", accessor="region_display", orderable=False)
    latest_year = Column(verbose_name="Last Year Debated", order_by="latest_year")

    class Meta:
        # Don't specify model since we're using custom objects
        fields = ("id", "first_name", "last_name", "school_name", "region", "latest_year")
        attrs = {"class": "table table-striped"}
        order_by = "latest_year"
    
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
    
    def render_latest_year(self, record):
        """Render the latest season - returns the full season string formatted as YYYY-YY"""
        if not record.latest_season:
            return ""
        # Format the season from "2024" to "2024-25"
        try:
            year = int(str(record.latest_season).split('-')[0])
            next_year = str(year + 1)[2:]
            return f"{year}-{next_year}"
        except (ValueError, IndexError):
            return record.latest_season  # Return as-is if parsing fails


class DinoJudgeListView(CustomListView):
    """List of graduated debaters open to judging opportunities"""
    public_view = True
    model = Debater
    table_class = DinoTable
    template_name = "debaters/judge_list.html"
    filterset_class = DebaterOutreachFilter

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
        
        # Sort by latest year (oldest first), then by name
        aggregated_dinos.sort(key=lambda d: (d.latest_season_year if d.latest_season_year else 9999, d.last_name, d.first_name))
        
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
    filterset_class = DebaterOutreachFilter

    def get_queryset(self):
        # Get all debaters (any status) with TO opt-in, apply filters first
        qs = Debater.objects.filter(
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
        
        # Sort by latest year (oldest first), then by name
        aggregated_dinos.sort(key=lambda d: (d.latest_season_year if d.latest_season_year else 9999, d.last_name, d.first_name))
        
        return aggregated_dinos
    
    def get_table_data(self):
        return self.get_queryset()
    
    def get_filterset(self, filterset_class):
        """Get the filterset instance, filtering on the base Debater queryset"""
        kwargs = self.get_filterset_kwargs(filterset_class)
        # Override the queryset to be the base Debater queryset (any status with TO opt-in)
        kwargs['queryset'] = Debater.objects.filter(
            dino_to_contact_opt_in=True
        )
        return filterset_class(**kwargs)
