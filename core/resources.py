from import_export import resources

from core.models import *


class SchoolResource(resources.ModelResource):
    class Meta:
        model = School


class DebaterResource(resources.ModelResource):
    class Meta:
        model = Debater


class TournamentResource(resources.ModelResource):
    class Meta:
        model = Tournament


class TeamResource(resources.ModelResource):
    class Meta:
        model = Team


class TeamResultResource(resources.ModelResource):
    class Meta:
        model = TeamResult
