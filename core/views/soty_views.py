from django.conf import settings
from django.shortcuts import redirect, reverse
from django_filters import FilterSet
from django_tables2 import Column

from core.models.standings.soty import SOTY
from core.utils.generics import (
    CustomListView,
    CustomTable,
    MarkerColumn,
    PlaceColumn,
    PointsColumn,
)


class SOTYFilter(FilterSet):
    class Meta:
        model = SOTY
        fields = {
            "debater__id": ["exact"],
            "debater__first_name": ["icontains"],
            "debater__last_name": ["icontains"],
            "debater__school__name": ["icontains"],
            "season": ["exact"],
        }


class SOTYTable(CustomTable):
    place = PlaceColumn()

    points = PointsColumn()

    marker_one = MarkerColumn(number="one", verbose_name="1")
    marker_two = MarkerColumn(number="two", verbose_name="2")
    marker_three = MarkerColumn(number="three", verbose_name="3")
    marker_four = MarkerColumn(number="four", verbose_name="4")
    marker_five = MarkerColumn(number="five", verbose_name="5")
    marker_six = MarkerColumn(number="six", verbose_name="6")

    debater = Column(
        verbose_name="Debater",
        accessor="debater__name",
        linkify=lambda record: record.debater.get_absolute_url(),
    )
    school = Column(
        verbose_name="School",
        accessor="debater__school__name",
        linkify=lambda record: record.debater.school.get_absolute_url(),
    )

    class Meta:
        model = SOTY
        fields = (
            "place",
            "debater",
            "school",
            "points",
            "marker_one",
            "marker_two",
            "marker_three",
            "marker_four",
            "marker_five",
            "marker_six",
        )


class SOTYListView(CustomListView):
    public_view = True
    model = SOTY
    table_class = SOTYTable
    template_name = "sotys/list.html"

    filterset_class = SOTYFilter

    def get(self, request, *args, **kwargs):
        if not self.request.GET.get("season"):
            return redirect(reverse("core:soty") + "?season=" + settings.CURRENT_SEASON)
        return super().get(request, *args, **kwargs)
