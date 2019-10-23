from django.db import models

from django.conf import settings

from core.models.school import School
from core.models.standings.base import AbstractStanding


class COTY(AbstractStanding):
    school = models.ForeignKey(School,
                               on_delete=models.CASCADE,
                               related_name='coty')

    class Meta:
        unique_together = ('season', 'school')
