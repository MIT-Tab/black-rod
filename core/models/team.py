from django.db import models
from django.shortcuts import reverse
from django.utils.html import format_html

from .debater import Debater


class Team(models.Model):
    name = models.CharField(max_length=128, blank=False)

    debaters = models.ManyToManyField(Debater, related_name="teams")

    def update_name(self):
        # Optimize by fetching debaters once with school prefetch
        debaters = list(self.debaters.select_related('school').all())
        self.update_name_from_debaters(debaters)
    
    def update_name_from_debaters(self, debaters):
        """Update team name from provided debater list (avoids additional DB queries)"""
        if not debaters:
            self.name = "Empty Team"
            return
            
        if len(debaters) == 1:
            self.name = f"{debaters[0].school.name} {debaters[0].last_name[0]}"
            return
            
        school_name = ""
        if debaters[0].school == debaters[1].school:
            school_name = debaters[0].school.name
        else:
            school_name = f"{debaters[0].school.name} / {debaters[1].school.name}"

        initials = ''.join([debater.last_name[0] for debater in debaters])
        self.name = f"{school_name} {initials}"

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
            names = f"{debaters[0].first_name} {debaters[0].last_name} and {debaters[1].first_name} {debaters[1].last_name}"
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