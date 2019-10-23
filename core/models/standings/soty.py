from django.db import models

from django.conf import settings

from core.models.debater import Debater
from core.models.standings.base import AbstractStanding


class SOTY(AbstractStanding):
    debater = models.ForeignKey(Debater,
                                on_delete=models.CASCADE,
                                related_name='soty')

    class Meta:
        unique_together = ('season', 'debater')
