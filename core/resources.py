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

class ReaffResource(resources.ModelResource):
    class Meta:
        model = Reaff


class TeamResource(resources.ModelResource):
    class Meta:
        model = Team


class TeamResultResource(resources.ModelResource):
    class Meta:
        model = TeamResult


class SpeakerResultResource(resources.ModelResource):
    class Meta:
        model = SpeakerResult


class NOTYResource(resources.ModelResource):
    class Meta:
        model = NOTY


class SOTYResource(resources.ModelResource):
    class Meta:
        model = SOTY


class TOTYResource(resources.ModelResource):
    class Meta:
        model = TOTY


class COTYResource(resources.ModelResource):
    class Meta:
        model = COTY


class QualPointsResource(resources.ModelResource):
    class Meta:
        model = QualPoints


class QUALResource(resources.ModelResource):
    class Meta:
        model = QUAL
