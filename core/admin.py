from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from import_export.admin import ImportExportModelAdmin

from core.models import *
from core.resources import *

# Register your models here.


class SchoolAdmin(ImportExportModelAdmin):
    resource_class = SchoolResource


admin.site.register(User, UserAdmin)

admin.site.register(School, SchoolAdmin)
