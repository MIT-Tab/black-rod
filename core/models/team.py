from django.db import models
from django.shortcuts import reverse

from django.conf import settings

from .school import School
from .debater import Debater


class Team(models.Model):
    name = models.CharField(max_length=128,
                            blank=False)

    debaters = models.ManyToManyField(Debater,
                                      related_name='teams')

    def update_name(self):
        school_name = ''

        if self.debaters.first().school == self.debaters.last().school:
            school_name = self.debaters.first().school.name
        else:
            school_name = '%s / %s' % (self.debaters.first().school.name,
                                       self.debaters.last().school.name)

        self.name = '%s %s' % (school_name,
                               ''.join([debater.last_name[0] \
                                        for debater in self.debaters.all()]))

    @property
    def debaters_display(self):
        return ', '.join(['<a href="%s">%s</a>' % (debater.get_absolute_url(),
                                                   debater.name) for debater in self.debaters.all()])

    @property
    def toty_points(self):
        return sum([t.points for t in self.toty.all()])

    def get_absolute_url(self):
        return reverse('core:team_detail', kwargs={'pk': self.id})

    def __str__(self):
        return self.name
