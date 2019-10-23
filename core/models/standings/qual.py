from django.db import models

from django.conf import settings

from core.models.debater import Debater
from core.models.standings.base import AbstractStanding


class QUAL(AbstractStanding):
    debater = models.ForeignKey(Debater,
                                on_delete=models.CASCADE,
                                related_name='quals')

    AUTOQUAL = 0
    BRANDEIS = 1
    YALE = 2
    NORTHAMS = 3
    EXPANSION = 4
    WORLDS = 5
    NAUDC = 6

    QUAL_TYPES = (
        (AUTOQUAL, 'Autoqual'),
        (BRANDEIS, 'Brandeis IV'),
        (YALE, 'Yale IV'),
        (NORTHAMS, 'NorthAms'),
        (EXPANSION, 'Expansion'),
        (WORLDS, 'Worlds'),
        (NAUDC, 'NAUDC')
    )

    qual_type = models.IntegerField(choices=QUAL_TYPES,
                                    default=AUTOQUAL)

    class Meta:
        unique_together = ('season', 'debater', 'qual_type')
