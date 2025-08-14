from django.db import models
from django.shortcuts import reverse
from django.utils.html import format_html

from .debater import Debater


class Team(models.Model):
    name = models.CharField(max_length=128, blank=False)

    debaters = models.ManyToManyField(Debater, related_name="teams")

    def update_name(self):
        school_name = ""

        if self.debaters.first().school == self.debaters.last().school:
            school_name = self.debaters.first().school.name
        else:
            school_name = f"{self.debaters.first().school.name} / {self.debaters.last().school.name}"

        self.name = f"{school_name} {''.join([debater.last_name[0] for debater in self.debaters.all()])}"

    @property
    def debaters_display(self):
        return format_html(
            " and ".join(
                [
                    f'<a href="{debater.get_absolute_url()}">{debater.name}</a>'
                    for debater in self.debaters.all()
                ]
            )
        )

    @property
    def long_name(self):
        debaters = list(self.debaters.all())
        if len(debaters) == 2:
            school = (
                debaters[0].school.name
                if debaters[0].school == debaters[1].school
                else f"{debaters[0].school.name} / {debaters[1].school.name}"
            )
            names = (
                f"{debaters[0].first_name} {debaters[0].last_name} and {debaters[1].first_name} {debaters[1].last_name}"
            )
            return f"{school} {names}"
        if debaters:
            return f"{debaters[0].school.name} {debaters[0].first_name} {debaters[0].last_name}"

        return self.name

    @property
    def toty_points(self):
        return sum([t.points for t in self.toty.all()])

    @property
    def hybrid(self):
        schools = list({d.school for d in self.debaters.all()})

        return len(schools) == 2

    def get_absolute_url(self):
        return reverse("core:team_detail", kwargs={"pk": self.id})

    def __str__(self):
        return self.name
