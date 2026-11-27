from django.db import models
from django.shortcuts import reverse
from django.utils.html import format_html

from .debater import Debater


class Team(models.Model):
    name = models.CharField(max_length=128, blank=False)
    short_name = models.CharField(max_length=128, blank=False, default="")
    synthetic = models.BooleanField(default=False, db_index=True)

    debaters = models.ManyToManyField(Debater, related_name="teams")

    def _naming_debaters(self):
        debater_ids = list(
            self.debaters.through.objects.filter(team_id=self.id)
            .order_by("id")
            .values_list("debater_id", flat=True)
        )
        if not debater_ids:
            return []

        debaters_by_id = {
            debater.id: debater
            for debater in Debater.all_objects.filter(id__in=debater_ids).select_related("school")
        }
        return [debaters_by_id[debater_id] for debater_id in debater_ids if debater_id in debaters_by_id]

    def _native_team_label(self, school_attr="name"):
        debaters = self._naming_debaters()
        if not debaters:
            return ""

        first = debaters[0]
        last = debaters[-1]
        first_school = (
            getattr(first.school, school_attr, "") or getattr(first.school, "name", "")
            if first.school
            else "Unaffiliated"
        )
        last_school = (
            getattr(last.school, school_attr, "") or getattr(last.school, "name", "")
            if last.school
            else "Unaffiliated"
        )

        if first.school_id and first.school_id == last.school_id:
            school_name = first_school
        elif first_school == last_school:
            school_name = first_school
        else:
            school_name = f"{first_school} / {last_school}"

        initials = "".join(
            (debater.last_name[:1] or debater.first_name[:1] or "?")
            for debater in debaters
        )
        return f"{school_name} {initials}".strip()

    def build_native_name(self):
        return self._native_team_label()

    def build_native_short_name(self):
        return self._native_team_label(school_attr="short_name")

    def update_name(self):
        name = self.build_native_name()
        if not name:
            return
        self.name = name

    @property
    def debaters_display(self):
        debaters = self.debaters.exclude(synthetic=True)
        return format_html(
            " and ".join(
                [
                    f'<a href="{debater.get_absolute_url()}">{debater.name}</a>'
                    for debater in debaters
                ]
            )
        )

    @property
    def long_name(self):
        debaters = list(self.debaters.all())
        if len(debaters) == 2:
            left_school = debaters[0].school.name if debaters[0].school else "Unaffiliated"
            right_school = debaters[1].school.name if debaters[1].school else "Unaffiliated"
            school = (
                left_school
                if debaters[0].school_id and debaters[0].school_id == debaters[1].school_id
                else left_school if left_school == right_school
                else f"{left_school} / {right_school}"
            )
            names = f"{debaters[0].first_name} {debaters[0].last_name} and {debaters[1].first_name} {debaters[1].last_name}"
            return f"{school} {names}"
        if debaters:
            school_name = debaters[0].school.name if debaters[0].school else "Unaffiliated"
            return f"{school_name} {debaters[0].first_name} {debaters[0].last_name}"

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
