from django.db import models
from django.shortcuts import reverse

from .school import School
from .debater import Debater


class Team(models.Model):
    school = models.ForeignKey(School,
                               on_delete=models.SET_NULL,
                               related_name='teams',
                               blank=True,
                               null=True)

    name = models.CharField(max_length=128,
                            blank=False)

    debaters = models.ManyToManyField(Debater)

    def update_name(self):
        self.name = '%s %s' % (self.school.name,
                               ''.join([debater.last_name[0] \
                                        for debater in self.debaters.all()]))

    def get_absolute_url(self):
        return reverse('core:team_detail', kwargs={'pk': self.id})

    def __str__(self):
        return self.name
