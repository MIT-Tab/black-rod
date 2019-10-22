from import_export import resources

from core.models import *


class SchoolResource(resources.ModelResource):
    class Meta:
        model = School
