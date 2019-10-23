from django.db import models

from django.conf import settings

from core.models.debater import Debater
from core.models.standings.base import AbstractStanding


class NOTY(AbstractStanding):
    debater = models.ForeignKey(Debater,
                                on_delete=models.CASCADE,
                                related_name='noty')

    class Meta:
        unique_together = ('season', 'debater')
