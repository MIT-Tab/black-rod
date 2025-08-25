from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
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
    QUAL,
    QualPoints,
    QualBar,
    Reaff,
    Round,
    RoundStats,
    School,
    SchoolLookup,
    SiteSetting,
    SOTY,
    SpeakerResult,
    Team,
    TeamResult,
    TOTY,
    TOTYReaff,
    Tournament,
    User,
    Video,
    Debater,
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
    list_display = ["name"]
    list_filter = ["name"]
    search_fields = ["name"]
    ordering = ["name"]


@admin.register(Debater)
class DebaterAdmin(ImportExportModelAdmin):
    resource_class = DebaterResource
    list_display = ("first_name", "last_name", "school", "id")
    list_filter = ("first_name", "last_name", "school", "id")
    search_fields = ("first_name", "last_name", "school", "id")
    ordering = ("first_name", "last_name", "school")

    @admin.display(
        description="Debater Name",
        ordering="debater__first_name",
    )
    def debater_name(self, obj):
        return f"{obj.debater.first_name} {obj.debater.last_name}"



@admin.register(Reaff)
class ReaffAdmin(ImportExportModelAdmin):
    resource_class = ReaffResource
    form = ReaffForm


@admin.register(Tournament)
class TournamentAdmin(ImportExportModelAdmin):
    resource_class = TournamentResource
    list_display = ["name", "host_name", "id", "season"]
    list_filter = ["name", "host__name", "id", "season"]
    search_fields = ["name", "host__name", "id", "season"]

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



@admin.register(Team)
class TeamAdmin(ImportExportModelAdmin):
    search_fields = ["name", "debaters__name", "debaters__school__name"]

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


@admin.register(SpeakerResult)
class SpeakerResultAdmin(ImportExportModelAdmin):
    resource_class = SpeakerResultResource


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



admin.site.register(Round)
admin.site.register(RoundStats)

admin.site.register(SchoolLookup)
