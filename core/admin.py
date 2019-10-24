from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from import_export.admin import ImportExportModelAdmin

from core.models import *
from core.resources import *

# Register your models here.


class SchoolAdmin(ImportExportModelAdmin):
    resource_class = SchoolResource


class DebaterAdmin(ImportExportModelAdmin):
    resource_class = DebaterResource


class TournamentAdmin(ImportExportModelAdmin):
    resource_class = TournamentResource


class TeamAdmin(ImportExportModelAdmin):
    resource_class = TeamResource


class TeamResultAdmin(ImportExportModelAdmin):
    resource_class = TeamResultResource


admin.site.register(User, UserAdmin)

admin.site.register(School, SchoolAdmin)
admin.site.register(Debater, DebaterAdmin)
admin.site.register(Tournament, TournamentAdmin)
admin.site.register(Team, TeamAdmin)
admin.site.register(TeamResult, TeamResultAdmin)
