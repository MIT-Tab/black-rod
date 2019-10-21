from django.db import models


class School(models.Model):
    name = models.CharField(max_length=64,
                            blank=False)

    included_in_oty = models.BooleanField(default=True)
