import json

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db.models import Count
from django.utils.html import format_html
from import_export.admin import ImportExportModelAdmin

from core.forms import (
    COTYForm,
    NOTYForm,
    QUALForm,
    QualPointsForm,

    ReaffForm,
    SOTYForm,
    TOTYReaffForm,
)
from core.models import (
    COTY,
    NOTY,
    OnlineQUAL,
    QUAL,
    QualPoints,
    QualBar,
    Reaff,
    Round,
    RoundStats,
    School,
    SchoolAdmin as SchoolAdminModel,
    SchoolLookup,
    SiteSetting,
    SOTY,
    SpeakerResult,
    Team,
    TeamResult,
    TaggedResource,
    TOTY,
    TOTYReaff,
    Tournament,
    TournamentImport,
    User,
    Video,
    Resource,
    ResourceTag,
    Debater,
    DebaterAlias,
    SchedulerWorkspace,
    SchedulingRun,
    MergeDebaterRequest,
    ClaimDebaterRequest,
    DebaterAliasGroup,
    ImportBatch,
    ImportedRoundJudge,
    ImportedRoundMetadata,
    SyntheticResolutionLog,
)
from core.resources import (
    COTYResource,
    DebaterResource,
    NOTYResource,
    QUALResource,
    QualPointsResource,
    ReaffResource,
    SchoolResource,
    SpeakerResultResource,
    TeamResultResource,
    TournamentResource,
)

# Register your models here.


@admin.register(School)
class SchoolAdmin(ImportExportModelAdmin):
    resource_class = SchoolResource
    list_display = ["id", "name", "short_name"]
    list_filter = ["name"]
    search_fields = ["name", "short_name"]
    ordering = ["name"]
    fields = ["name", "short_name", "included_in_oty"]


@admin.register(Debater)
class DebaterAdmin(ImportExportModelAdmin):
    resource_class = DebaterResource
    list_display = ("first_name", "last_name", "school", "synthetic", "temporary", "id")
    list_filter = ("synthetic", "temporary", "first_name", "last_name", "school", "id")
    search_fields = ("first_name", "last_name", "school__name", "id")
    ordering = ("first_name", "last_name", "school")

    def get_queryset(self, request):
        return Debater.all_objects.select_related("school")

    @admin.display(
        description="Debater Name",
        ordering="first_name",
    )
    def debater_name(self, obj):
        return f"{obj.first_name} {obj.last_name}"


@admin.register(DebaterAliasGroup)
class DebaterAliasGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "id")
    search_fields = ("label",)
    ordering = ("label", "id")


@admin.register(DebaterAlias)
class DebaterAliasAdmin(admin.ModelAdmin):
    list_display = (
        "source_name",
        "normalized_name",
        "debater",
        "created_at",
        "updated_at",
    )
    list_filter = ("created_at", "updated_at")
    search_fields = (
        "source_name",
        "normalized_name",
        "debater__first_name",
        "debater__last_name",
    )
    ordering = ("source_name", "id")
    list_select_related = ("debater",)
    raw_id_fields = ("debater",)



@admin.register(Reaff)
class ReaffAdmin(ImportExportModelAdmin):
    resource_class = ReaffResource
    form = ReaffForm


@admin.register(Tournament)
class TournamentAdmin(ImportExportModelAdmin):
    resource_class = TournamentResource
    list_display = ["name", "short_name", "host_name", "id", "season"]
    list_filter = ["name", "host__name", "id", "season"]
    search_fields = ["name", "short_name", "host__name", "id", "season"]

    @admin.display(
        description="Host Name",
        ordering="host__name",
    )
    def host_name(self, obj):
        return obj.host.name if obj.host else ""



@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ('Permissions', {'fields': ('can_view_private_videos',)}),
    )

    list_display = UserAdmin.list_display + ('can_view_private_videos',)

    list_filter = UserAdmin.list_filter + ('can_view_private_videos',)


@admin.register(SchedulerWorkspace)
class SchedulerWorkspaceAdmin(admin.ModelAdmin):
    list_display = ("name", "version", "updated_at", "updated_by")
    readonly_fields = ("created_at", "updated_at")


@admin.register(SchedulingRun)
class SchedulingRunAdmin(admin.ModelAdmin):
    list_display = ("id", "workspace", "status", "created_at", "created_by")
    list_filter = ("status",)
    readonly_fields = ("created_at", "completed_at")



@admin.register(Team)
class TeamAdmin(ImportExportModelAdmin):
    list_display = ["name", "short_name", "id"]
    search_fields = [
        "name",
        "short_name",
        "debaters__first_name",
        "debaters__last_name",
        "debaters__school__name",
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .prefetch_related("debaters", "debaters__school")
        )

    def get_search_results(self, request, queryset, search_term):
        queryset, use_distinct = super().get_search_results(
            request, queryset, search_term
        )
        return queryset, use_distinct


@admin.register(TeamResult)
class TeamResultAdmin(ImportExportModelAdmin):
    resource_class = TeamResultResource
    list_display = (
        "tournament_name",
        "tournament_season",
        "team_name",
        "type_of_place_display",
        "place",
        "ghost_points",
    )
    list_filter = (
        "tournament__name",
        "tournament__season",
        "team__name",
        "type_of_place",
        "ghost_points",
    )
    search_fields = (
        "tournament__name",
        "tournament__season",
        "team__name",
        "place",
    )
    ordering = ("tournament__name", "tournament__season", "team__name", "type_of_place", "place")

    @admin.display(description="Tournament", ordering="tournament__name")
    def tournament_name(self, obj):
        return obj.tournament.name

    @admin.display(description="Season", ordering="tournament__season")
    def tournament_season(self, obj):
        return obj.tournament.season

    @admin.display(description="Team", ordering="team__name")
    def team_name(self, obj):
        return obj.team.name

    @admin.display(description="Type of Place", ordering="type_of_place")
    def type_of_place_display(self, obj):
        return obj.get_type_of_place_display()

    @admin.display(description="Ghost Points", ordering="ghost_points")
    def ghost_points(self, obj):
        return obj.ghost_points


@admin.register(SpeakerResult)
class SpeakerResultAdmin(ImportExportModelAdmin):
    resource_class = SpeakerResultResource


@admin.register(MergeDebaterRequest)
class MergeDebaterRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "primary_debater",
        "secondary_debater",
        "status",
        "requested_by",
        "created_at",
        "processed_by",
        "processed_at",
    )
    list_filter = ("status", "created_at", "processed_at")
    search_fields = (
        "primary_debater__first_name",
        "primary_debater__last_name",
        "secondary_debater__first_name",
        "secondary_debater__last_name",
        "requested_by__username",
    )
    ordering = ("status", "created_at")
    raw_id_fields = (
        "primary_debater",
        "secondary_debater",
        "requested_by",
        "processed_by",
    )


@admin.register(ClaimDebaterRequest)
class ClaimDebaterRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "debater",
        "requested_by",
        "status",
        "created_at",
        "processed_by",
        "processed_at",
    )
    list_filter = ("status", "created_at", "processed_at")
    search_fields = (
        "debater__first_name",
        "debater__last_name",
        "requested_by__username",
        "requested_by__email",
    )
    ordering = ("-created_at",)
    raw_id_fields = (
        "debater",
        "requested_by",
        "processed_by",
    )
    readonly_fields = ("created_at", "processed_at")


@admin.register(NOTY)
class NOTYAdmin(ImportExportModelAdmin):
    resource_class = NOTYResource
    form = NOTYForm


@admin.register(SOTY)
class SOTYAdmin(ImportExportModelAdmin):
    resource_class = SpeakerResultResource
    form = SOTYForm
    list_display = ("debater_name", "season", "place", "marker_one", "marker_two")
    list_filter = ("debater__first_name", "debater__last_name", "season")
    search_fields = ("debater__first_name", "debater__last_name")
    ordering = ("debater__first_name", "debater__last_name")

    @admin.display(
        description="Debater Name",
        ordering="debater__first_name",
    )
    def debater_name(self, obj):
        return f"{obj.debater.first_name} {obj.debater.last_name}"



@admin.register(TOTY)
class TOTYAdmin(admin.ModelAdmin):
    list_display = (
        "team_name",
        "debater_names",
        "season",
        "points",
        "place",
    )
    search_fields = (
        "team__name",
        "team__debaters__first_name",
        "team__debaters__last_name",
    )

    @admin.display(
        description="Team"
    )
    def team_name(self, obj):
        return obj.team.name

    @admin.display(
        description="Debaters"
    )
    def debater_names(self, obj):
        return ", ".join([debater.name for debater in obj.team.debaters.all()])

    @admin.display(
        description="School"
    )
    def school_name(self, obj):
        return obj.team.school.name



@admin.register(TOTYReaff)
class TOTYReaffAdmin(admin.ModelAdmin):
    form = TOTYReaffForm
    autocomplete_fields = ["old_team", "new_team"]
    list_display = (
        "season",
        "old_team_name",
        "old_debaters",
        "new_team_name",
        "new_debaters",
        "reaff_date",
    )
    search_fields = (
        "old_team__name",
        "old_team__debaters__name",
        "new_team__name",
        "new_team__debaters__name",
    )

    @admin.display(
        description="Old Team"
    )
    def old_team_name(self, obj):
        return obj.old_team.debaters_display

    @admin.display(
        description="New Team"
    )
    def new_team_name(self, obj):
        return obj.new_team.debaters_display

    @admin.display(
        description="Old Debaters"
    )
    def old_debaters(self, obj):
        return ", ".join([debater.name for debater in obj.old_team.debaters.all()])

    @admin.display(
        description="New Debaters"
    )
    def new_debaters(self, obj):
        return ", ".join([debater.name for debater in obj.new_team.debaters.all()])



@admin.register(COTY)
class COTYAdmin(ImportExportModelAdmin):
    resource_class = COTYResource
    form = COTYForm
    list_display = ("school_name", "season", "place")
    list_filter = ("season", "place")
    search_fields = ("school__name", "season")
    ordering = ("school__name", "season")

    @admin.display(
        description="School Name",
        ordering="school__name",
    )
    def school_name(self, obj):
        return obj.school.name



@admin.register(QualPoints)
class QualPointsAdmin(ImportExportModelAdmin):
    resource_class = QualPointsResource
    form = QualPointsForm
    list_display = ("debater_name", "season", "points")
    list_filter = ("debater__first_name", "debater__last_name", "season")
    search_fields = ("debater__first_name", "debater__last_name")
    ordering = ("debater__first_name", "debater__last_name")

    @admin.display(
        description="Debater Name",
        ordering="debater__first_name",
    )
    def debater_name(self, obj):
        return f"{obj.debater.first_name} {obj.debater.last_name}"



@admin.register(QUAL)
class QUALAdmin(ImportExportModelAdmin):
    resource_class = QUALResource
    form = QUALForm
    list_display = ("debater_name", "season", "place", "id")
    list_filter = ("debater__first_name", "debater__last_name", "season")
    search_fields = ("debater__first_name", "debater__last_name", "id")
    ordering = ("debater__first_name", "debater__last_name")

    @admin.display(
        description="Debater Name",
        ordering="debater__first_name",
    )
    def debater_name(self, obj):
        return f"{obj.debater.first_name} {obj.debater.last_name}"



@admin.register(SiteSetting)
class SiteSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "value")
    search_fields = ("key",)
    ordering = ("key",)

@admin.register(QualBar)
class QualBarAdmin(admin.ModelAdmin):
    list_display = ("season", "points")
    search_fields = ("season",)


@admin.register(OnlineQUAL)
class OnlineQUALAdmin(admin.ModelAdmin):
    list_display = (
        "debater_name",
        "season",
        "place",
        "points",
        "marker_one",
        "marker_two",
    )
    list_filter = ("season", "place", "tied")
    search_fields = ("debater__first_name", "debater__last_name")
    ordering = ("season", "place", "debater__first_name", "debater__last_name")
    list_select_related = ("debater",)
    raw_id_fields = (
        "debater",
        "tournament_one",
        "tournament_two",
        "tournament_three",
        "tournament_four",
        "tournament_five",
        "tournament_six",
    )

    @admin.display(
        description="Debater Name",
        ordering="debater__first_name",
    )
    def debater_name(self, obj):
        return f"{obj.debater.first_name} {obj.debater.last_name}"


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ("tournament", "round", "pm", "lo", "mg", "mo", "permissions")
    list_filter = ("tournament", "round", "permissions", "tags")
    search_fields = (
        "tournament__name",
        "pm__name",
        "lo__name",
        "mg__name",
        "mo__name",
        "case",
        "description",
    )
    autocomplete_fields = ("pm", "lo", "mg", "mo", "tournament")
    readonly_fields = ("get_absolute_url",)
    filter_horizontal = ("tags",)
    fieldsets = (
        ("Debaters", {"fields": ("pm", "lo", "mg", "mo")}),
        ("Tournament & Round", {"fields": ("tournament", "round")}),
        ("Video Details", {"fields": ("link", "password", "case", "description")}),
        ("Permissions & Tags", {"fields": ("permissions", "tags")}),
    )

    @admin.display(
        description="Video URL"
    )
    def get_absolute_url(self, obj):
        return obj.get_absolute_url()


@admin.register(Resource)
class ResourceAdmin(admin.ModelAdmin):
    list_display = ("title", "resource_type", "viewing_permission", "created_at", "created_by")
    list_filter = ("resource_type", "viewing_permission", "tags", "created_at")
    search_fields = (
        "title",
        "description",
        "usage_permissions",
        "authors__first_name",
        "authors__last_name",
    )
    filter_horizontal = ("authors", "tags")
    readonly_fields = ("created_at", "updated_at", "get_absolute_url")
    fieldsets = (
        ("Basic Information", {"fields": ("title", "resource_type", "authors")}),
        ("Content", {"fields": ("content_link", "description", "usage_permissions")}),
        ("Permissions & Tags", {"fields": ("viewing_permission", "tags")}),
        ("Metadata", {"fields": ("created_by", "created_at", "updated_at", "get_absolute_url")}),
    )

    @admin.display(
        description="Resource URL"
    )
    def get_absolute_url(self, obj):
        return obj.get_absolute_url()


class RoundStatsInline(admin.TabularInline):
    model = RoundStats
    extra = 0
    autocomplete_fields = ("debater",)
    fields = ("debater", "debater_role", "score_index", "speaks", "ranks", "source_status")
    ordering = ("score_index", "id")
    show_change_link = True


class ImportedRoundMetadataInline(admin.StackedInline):
    model = ImportedRoundMetadata
    extra = 0
    max_num = 1
    autocomplete_fields = (
        "gov_1_alias",
        "gov_2_alias",
        "opp_1_alias",
        "opp_2_alias",
        "sources",
    )
    fields = (
        ("gov_1_alias", "gov_1_role"),
        ("gov_2_alias", "gov_2_role"),
        ("opp_1_alias", "opp_1_role"),
        ("opp_2_alias", "opp_2_role"),
        "raw_result_code",
        "raw_outcome_text",
        "sources",
    )
    show_change_link = True


@admin.register(Round)
class RoundAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "tournament_name",
        "round_summary",
        "stage",
        "division",
        "gov",
        "opp",
        "victor_display",
        "is_rated",
        "weight",
        "stats_count",
        "import_origin",
    )
    list_filter = ("stage", "division", "is_rated", "victor", "import_origin", "tournament__season")
    search_fields = (
        "=id",
        "round_label",
        "import_key",
        "tournament__name",
        "tournament__short_name",
        "gov__name",
        "opp__name",
        "gov__debaters__first_name",
        "gov__debaters__last_name",
        "opp__debaters__first_name",
        "opp__debaters__last_name",
    )
    ordering = ("-tournament__date", "-tournament_id", "round_number", "id")
    list_select_related = ("tournament", "gov", "opp")
    autocomplete_fields = ("tournament", "gov", "opp")
    readonly_fields = ("stats_count", "imported_metadata_summary", "metadata_pretty")
    fieldsets = (
        ("Round", {"fields": ("tournament", "round_number", "round_label", "stage", "division", "elim_size")}),
        ("Matchup", {"fields": ("gov", "opp", "victor", "is_rated", "weight", "stats_count")}),
        ("Import", {"fields": ("import_origin", "import_key", "imported_metadata_summary")}),
        ("Metadata", {"fields": ("metadata_pretty",), "classes": ("collapse",)}),
    )
    inlines = (RoundStatsInline, ImportedRoundMetadataInline)
    save_on_top = True

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("tournament", "gov", "opp", "imported_metadata")
            .annotate(_stats_count=Count("stats", distinct=True))
        )

    @admin.display(description="Tournament", ordering="tournament__name")
    def tournament_name(self, obj):
        return obj.tournament.name

    @admin.display(description="Round", ordering="round_number")
    def round_summary(self, obj):
        if obj.round_label:
            return f"{obj.round_label} (#{obj.round_number})"
        return f"Round {obj.round_number}"

    @admin.display(description="Victor", ordering="victor")
    def victor_display(self, obj):
        return obj.get_victor_display()

    @admin.display(description="Stats", ordering="_stats_count")
    def stats_count(self, obj):
        return getattr(obj, "_stats_count", obj.stats.count())

    @admin.display(description="Imported Metadata")
    def imported_metadata_summary(self, obj):
        try:
            metadata = obj.imported_metadata
        except ImportedRoundMetadata.DoesNotExist:
            return "No imported metadata"

        summary = []
        if metadata.raw_result_code:
            summary.append(f"Result code: {metadata.raw_result_code}")
        gov_aliases = [
            alias.source_name
            for alias in (metadata.gov_1_alias, metadata.gov_2_alias)
            if alias is not None
        ]
        opp_aliases = [
            alias.source_name
            for alias in (metadata.opp_1_alias, metadata.opp_2_alias)
            if alias is not None
        ]
        if gov_aliases:
            summary.append(f"Gov aliases: {', '.join(gov_aliases)}")
        if opp_aliases:
            summary.append(f"Opp aliases: {', '.join(opp_aliases)}")
        if metadata.raw_outcome_text:
            summary.append(metadata.raw_outcome_text)
        return " | ".join(summary) or "Imported metadata present"

    @admin.display(description="Metadata JSON")
    def metadata_pretty(self, obj):
        if not obj.metadata:
            return "No metadata"
        return format_html(
            "<pre>{}</pre>",
            json.dumps(obj.metadata, indent=2, sort_keys=True),
        )


@admin.register(RoundStats)
class RoundStatsAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "tournament_name",
        "round_summary",
        "debater",
        "debater_role",
        "score_index",
        "speaks",
        "ranks",
        "source_status",
    )
    list_filter = (
        "debater_role",
        "source_status",
        "round__stage",
        "round__division",
        "round__is_rated",
        "round__tournament__season",
    )
    search_fields = (
        "=id",
        "debater__first_name",
        "debater__last_name",
        "debater__school__name",
        "round__tournament__name",
        "round__round_label",
        "round__import_key",
    )
    ordering = ("-round__tournament__date", "-round_id", "score_index", "id")
    list_select_related = ("round__tournament", "debater", "debater__school")
    autocomplete_fields = ("round", "debater")
    readonly_fields = ("metadata_pretty",)
    fields = (
        "round",
        "debater",
        "debater_role",
        "score_index",
        "speaks",
        "ranks",
        "source_status",
        "metadata_pretty",
    )

    @admin.display(description="Tournament", ordering="round__tournament__name")
    def tournament_name(self, obj):
        return obj.round.tournament.name

    @admin.display(description="Round", ordering="round__round_number")
    def round_summary(self, obj):
        label = obj.round.round_label or f"Round {obj.round.round_number}"
        return f"{label} / {obj.round.gov} vs {obj.round.opp}"

    @admin.display(description="Metadata JSON")
    def metadata_pretty(self, obj):
        if not obj.metadata:
            return "No metadata"
        return format_html(
            "<pre>{}</pre>",
            json.dumps(obj.metadata, indent=2, sort_keys=True),
        )


admin.site.register(SchoolLookup)


@admin.register(SchoolAdminModel)
class SchoolAdminAdmin(admin.ModelAdmin):
    list_display = ("user", "school", "primary", "created_at")
    list_filter = ("school", "primary", "created_at")
    search_fields = ("user__username", "user__email", "school__name")
    autocomplete_fields = ("user", "school")
    ordering = ("school__name", "user__username")


@admin.register(ResourceTag)
class ResourceTagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    search_fields = ("name", "slug")
    ordering = ("name",)


@admin.register(TaggedResource)
class TaggedResourceAdmin(admin.ModelAdmin):
    list_display = ("id", "tag", "content_type", "object_id")
    list_filter = ("content_type", "tag")
    search_fields = ("tag__name", "tag__slug")
    ordering = ("content_type", "object_id", "tag__name")
    raw_id_fields = ("tag",)


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "tournament_import_count")
    ordering = ("-created_at", "-id")
    readonly_fields = ("created_at",)

    @admin.display(description="Tournament Imports")
    def tournament_import_count(self, obj):
        return obj.tournament_imports.count()


@admin.register(TournamentImport)
class TournamentImportAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "tournament",
        "import_type",
        "original_file_name",
        "source_hash",
        "batch",
        "imported_at",
    )
    list_filter = ("import_type", "imported_at")
    search_fields = ("tournament__name", "original_file_name", "source_hash")
    ordering = ("tournament__name", "-imported_at", "-id")
    list_select_related = ("tournament", "batch")
    raw_id_fields = ("tournament", "batch")
    readonly_fields = ("imported_at",)


@admin.register(ImportedRoundMetadata)
class ImportedRoundMetadataAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "round",
        "tournament_name",
        "gov_alias_summary",
        "opp_alias_summary",
        "raw_result_code",
    )
    search_fields = (
        "round__tournament__name",
        "round__import_key",
        "gov_1_alias__source_name",
        "gov_2_alias__source_name",
        "opp_1_alias__source_name",
        "opp_2_alias__source_name",
        "raw_result_code",
        "raw_outcome_text",
    )
    ordering = ("round__tournament__name", "round__round_number", "id")
    list_select_related = (
        "round__tournament",
        "gov_1_alias",
        "gov_2_alias",
        "opp_1_alias",
        "opp_2_alias",
    )
    raw_id_fields = (
        "round",
        "gov_1_alias",
        "gov_2_alias",
        "opp_1_alias",
        "opp_2_alias",
        "sources",
    )

    @admin.display(description="Tournament", ordering="round__tournament__name")
    def tournament_name(self, obj):
        return obj.round.tournament.name

    @admin.display(description="Gov Aliases")
    def gov_alias_summary(self, obj):
        return ", ".join(
            alias.source_name
            for alias in (obj.gov_1_alias, obj.gov_2_alias)
            if alias is not None
        ) or "-"

    @admin.display(description="Opp Aliases")
    def opp_alias_summary(self, obj):
        return ", ".join(
            alias.source_name
            for alias in (obj.opp_1_alias, obj.opp_2_alias)
            if alias is not None
        ) or "-"


@admin.register(ImportedRoundJudge)
class ImportedRoundJudgeAdmin(admin.ModelAdmin):
    list_display = ("id", "original_name", "round_metadata", "debater_alias", "is_chair")
    list_filter = ("is_chair",)
    search_fields = (
        "original_name",
        "debater_alias__source_name",
        "debater_alias__debater__first_name",
        "debater_alias__debater__last_name",
        "round_metadata__round__tournament__name",
    )
    ordering = (
        "round_metadata__round__tournament__name",
        "round_metadata__round__round_number",
        "id",
    )
    list_select_related = ("round_metadata__round__tournament", "debater_alias__debater")
    raw_id_fields = ("round_metadata", "debater_alias")


@admin.register(SyntheticResolutionLog)
class SyntheticResolutionLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "entity_type",
        "synthetic_id",
        "resolved_to_id",
        "actor",
        "created_at",
    )
    list_filter = ("entity_type", "created_at")
    search_fields = (
        "synthetic_name",
        "resolved_to_name",
        "reason",
        "synthetic_id",
        "resolved_to_id",
    )
    readonly_fields = (
        "entity_type",
        "synthetic_id",
        "synthetic_name",
        "resolved_to_id",
        "resolved_to_name",
        "actor",
        "reason",
        "source_context",
        "synthetic_snapshot",
        "created_at",
    )
