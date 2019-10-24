from django.db import models
from django.shortcuts import reverse

from .school import School
from .debater import Debater


class Team(models.Model):
    name = models.CharField(max_length=128,
                            blank=False)

    debaters = models.ManyToManyField(Debater,
                                      related_name='teams')

    def update_name(self):
        school_name = ''

        if self.debaters.all()[0].school == self.debaters.all()[1].school:
            school_name = self.debaters.all()[0].school.name
        else:
            school_name = '%s / %s' % (self.debaters.first().school.name,
                                       self.debaters.last().school.name)

        self.name = '%s %s' % (school_name,
                               ''.join([debater.last_name[0] \
                                        for debater in self.debaters.all()]))

    def get_absolute_url(self):
        return reverse('core:team_detail', kwargs={'pk': self.id})

    def __str__(self):
        return self.name
