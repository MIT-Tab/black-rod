from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from import_export.admin import ImportExportModelAdmin

from core.forms import TOTYReaffForm, QualPointsForm, ReaffForm, SOTYForm, NOTYForm, COTYForm, QUALForm
from core.models import *
from core.resources import *

# Register your models here.


class SchoolAdmin(ImportExportModelAdmin):
    resource_class = SchoolResource
    list_display = ['name']
    list_filter = ['name']
    search_fields = ['name']
    ordering = ['name']


class DebaterAdmin(ImportExportModelAdmin):
    resource_class = DebaterResource
    list_display = ('first_name', 'last_name', 'school', 'id')
    list_filter = ('first_name', 'last_name', 'school', 'id')
    search_fields = ('first_name', 'last_name', 'school', 'id')
    ordering = ('first_name', 'last_name', 'school')

    def debater_name(self, obj):
        return f"{obj.debater.first_name} {obj.debater.last_name}"
    
    debater_name.admin_order_field = 'debater__first_name'
    debater_name.short_description = 'Debater Name' 

class ReaffAdmin(ImportExportModelAdmin):
    resource_class = ReaffResource
    form = ReaffForm


class TournamentAdmin(ImportExportModelAdmin):
    resource_class = TournamentResource
    list_display = ['name']
    list_filter = ['name']
    search_fields = ['name']


class TeamAdmin(ImportExportModelAdmin):
    search_fields = ['name', 'debaters__name', 'debaters__school__name']

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('debaters', 'debaters__school')

    def get_search_results(self, request, queryset, search_term):
        queryset, use_distinct = super().get_search_results(request, queryset, search_term)
        return queryset, use_distinct


class TeamResultAdmin(ImportExportModelAdmin):
    resource_class = TeamResultResource


class SpeakerResultAdmin(ImportExportModelAdmin):
    resource_class = SpeakerResultResource


class NOTYAdmin(ImportExportModelAdmin):
    resource_class = NOTYResource
    form = NOTYForm


class SOTYAdmin(ImportExportModelAdmin):
    resource_class = SpeakerResultResource
    form = SOTYForm
    list_display = ('debater_name', 'season', 'place', 'marker_one', 'marker_two')
    list_filter = ('debater__first_name', 'debater__last_name', 'season')
    search_fields = ('debater__first_name', 'debater__last_name')
    ordering = ('debater__first_name', 'debater__last_name') 

    def debater_name(self, obj):
        return f"{obj.debater.first_name} {obj.debater.last_name}"
    
    debater_name.admin_order_field = 'debater__first_name'
    debater_name.short_description = 'Debater Name' 


class TOTYAdmin(admin.ModelAdmin):
    list_display = (
        'team_name', 'debater_names',
        'season', 'points', 'place',
    )
    search_fields = ('team__name', 'team__debaters__first_name', 'team__debaters__last_name')

    def team_name(self, obj):
        return obj.team.name

    def debater_names(self, obj):
        return ', '.join([debater.name for debater in obj.team.debaters.all()])

    def school_name(self, obj):
        return obj.team.school.name

    team_name.short_description = 'Team'
    debater_names.short_description = 'Debaters'
    school_name.short_description = 'School'


class TOTYReaffAdmin(admin.ModelAdmin):
    form = TOTYReaffForm
    autocomplete_fields = ['old_team', 'new_team']
    list_display = (
        'season', 'old_team_name', 'old_debaters',
        'new_team_name', 'new_debaters', 'reaff_date'
    )
    search_fields = (
        'old_team__name', 'old_team__debaters__name',
        'new_team__name', 'new_team__debaters__name'
    )

    def old_team_name(self, obj):
        return obj.old_team.debaters_display
    def new_team_name(self, obj):
        return obj.new_team.debaters_display

    def old_debaters(self, obj):
        return ', '.join([debater.name for debater in obj.old_team.debaters.all()])

    def new_debaters(self, obj):
        return ', '.join([debater.name for debater in obj.new_team.debaters.all()])


    old_team_name.short_description = 'Old Team'
    new_team_name.short_description = 'New Team'
    old_debaters.short_description = 'Old Debaters'
    new_debaters.short_description = 'New Debaters'

class COTYAdmin(ImportExportModelAdmin):
    resource_class = COTYResource
    form = COTYForm
    list_display = ('school_name', 'season', 'place')
    list_filter = ('season', 'place')
    search_fields = ('school__name', 'season')
    ordering = ('school__name', 'season')
    search_fields = ('school__name', 'season')
    ordering = ('school__name', 'season')

    def school_name(self, obj):
        return obj.school.name
    school_name.admin_order_field = 'school__name'  
    school_name.short_description = 'School Name'

    
class QualPointsAdmin(ImportExportModelAdmin):
    resource_class = QualPointsResource
    form = QualPointsForm
    list_display = ('debater_name', 'season', 'points')
    list_filter = ('debater__first_name', 'debater__last_name', 'season')
    search_fields = ('debater__first_name', 'debater__last_name')
    ordering = ('debater__first_name', 'debater__last_name') 

    def debater_name(self, obj):
        return f"{obj.debater.first_name} {obj.debater.last_name}"
    
    debater_name.admin_order_field = 'debater__first_name'
    debater_name.short_description = 'Debater Name' 
class QUALAdmin(ImportExportModelAdmin):
    resource_class = QUALResource
    form = QUALForm
    list_display = ('debater_name', 'season', 'place', 'id')
    list_filter = ('debater__first_name', 'debater__last_name', 'season')
    search_fields = ('debater__first_name', 'debater__last_name', 'id')
    ordering = ('debater__first_name', 'debater__last_name')
    list_filter = ('debater__first_name', 'debater__last_name', 'season')
    search_fields = ('debater__first_name', 'debater__last_name', 'id')
    ordering = ('debater__first_name', 'debater__last_name') 

    def debater_name(self, obj):
        return f"{obj.debater.first_name} {obj.debater.last_name}"
    
    debater_name.admin_order_field = 'debater__first_name'
    debater_name.short_description = 'Debater Name' 

class SiteSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "value")
    search_fields = ("key",)
    ordering = ("key",)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if obj.key == "CURRENT_SEASON":
            from django.conf import settings
            settings.CURRENT_SEASON = obj.value

class VideoAdmin(admin.ModelAdmin):
    list_display = ('tournament', 'round', 'pm', 'lo', 'mg', 'mo', 'permissions')
    list_filter = ('tournament', 'round', 'permissions', 'tags')
    search_fields = ('tournament__name', 'pm__name', 'lo__name', 'mg__name', 'mo__name', 'case', 'description')
    autocomplete_fields = ('pm', 'lo', 'mg', 'mo', 'tournament')
    readonly_fields = ('get_absolute_url',)
    filter_horizontal = ('tags',)
    fieldsets = (
        ('Debaters', {
            'fields': ('pm', 'lo', 'mg', 'mo')
        }),
        ('Tournament & Round', {
            'fields': ('tournament', 'round')
        }),
        ('Video Details', {
            'fields': ('link', 'password', 'case', 'description')
        }),
        ('Permissions & Tags', {
            'fields': ('permissions', 'tags')
        }),
    )

    def get_absolute_url(self, obj):
        return obj.get_absolute_url()

    get_absolute_url.short_description = "Video URL"


admin.site.register(User, UserAdmin)
admin.site.register(Round)
admin.site.register(RoundStats)
admin.site.register(SiteSetting, SiteSettingAdmin)

admin.site.register(SchoolLookup)
admin.site.register(Video, VideoAdmin)

admin.site.register(School, SchoolAdmin)
admin.site.register(Debater, DebaterAdmin)
admin.site.register(Reaff, ReaffAdmin)
admin.site.register(Tournament, TournamentAdmin)
admin.site.register(Team, TeamAdmin)
admin.site.register(TeamResult, TeamResultAdmin)
admin.site.register(SpeakerResult, SpeakerResultAdmin)

admin.site.register(SOTY, SOTYAdmin)
admin.site.register(NOTY, NOTYAdmin)
admin.site.register(TOTY, TOTYAdmin)
admin.site.register(TOTYReaff, TOTYReaffAdmin)
admin.site.register(COTY, COTYAdmin)
admin.site.register(QualPoints, QualPointsAdmin)
admin.site.register(QUAL, QUALAdmin)
