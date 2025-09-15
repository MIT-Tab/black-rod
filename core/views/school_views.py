from dal import autocomplete
from django.conf import settings
from django.shortcuts import redirect, reverse
from django.urls import reverse_lazy
from django_filters import FilterSet
from django_tables2 import Column

from core.models.school import School
from core.utils.generics import (
    CustomCreateView,
    CustomDeleteView,
    CustomDetailView,
    CustomListView,
    CustomTable,
    CustomUpdateView,
)
from core.utils.rankings import get_qualled_debaters
from core.utils.schools import get_debaters_for_season


class SchoolFilter(FilterSet):
    class Meta:
        model = School
        fields = {
            "id": ["exact"],
            "name": ["icontains"],
        }


class SchoolTable(CustomTable):
    id = Column(linkify=True)
    name = Column(linkify=True)

    included_in_oty = Column(verbose_name="APDA Member?")

    class Meta:
        model = School
        fields = ("id", "name", "included_in_oty")


class SchoolListView(CustomListView):
    public_view = True
    model = School
    table_class = SchoolTable
    template_name = "schools/list.html"

    filterset_class = SchoolFilter

    buttons = [
        {
            "name": "Create",
            "href": reverse_lazy("core:school_create"),
            "perm": "core.add_school",
            "class": "btn-success",
        }
    ]


class SchoolDetailView(CustomDetailView):
    public_view = True
    model = School
    template_name = "schools/detail.html"

    buttons = [
        {
            "name": "Delete",
            "href": "core:school_delete",
            "perm": "core.remove_school",
            "class": "btn-danger",
            "include_pk": True,
        },
        {
            "name": "Edit",
            "href": "core:school_update",
            "perm": "core.change_school",
            "class": "btn-info",
            "include_pk": True,
        },
    ]

    def get(self, request, *args, **kwargs):
        season = self.request.GET.get("season", "")
        if season == "":
            return redirect(
                reverse("core:school_detail", kwargs={"pk": self.get_object().id})
                + f"?season={settings.CURRENT_SEASON}"
            )
        return super().get(request, *args, **kwargs)

    def get_context_data(self, *args, **kwargs):
        season = self.request.GET.get("season")
        default = self.request.GET.get("default", "members")
        context = super().get_context_data(*args, **kwargs)

        context["cotys"] = self.object.coty.order_by("-season")

        context["quals"] = get_qualled_debaters(
            self.object, season
        )

        context["debaters"] = get_debaters_for_season(self.object, season)

        context["seasons"] = settings.SEASONS
        context["current_season"] = season
        context["default"] = default

        context["tournaments"] = self.object.hosted_tournaments.order_by("-date")

        return context


class SchoolUpdateView(CustomUpdateView):
    model = School

    fields = ["name", "included_in_oty"]
    template_name = "schools/update.html"

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        context["cotys"] = self.object.coty.order_by("-season")

        context["tournaments"] = self.object.hosted_tournaments.order_by("-date")

        return context


class SchoolCreateView(CustomCreateView):
    model = School

    fields = ["name", "included_in_oty"]
    template_name = "schools/create.html"


class SchoolDeleteView(CustomDeleteView):
    model = School
    success_url = reverse_lazy("core:school_list")

    template_name = "schools/delete.html"


class SchoolAutocomplete(autocomplete.Select2QuerySetView):
    def get_result_label(self, record):
        return f"<{record.id}> {record.name}"

    def get_queryset(self):
        qs = School.objects.all()

        if self.q:
            qs = qs.filter(name__icontains=self.q)

        return qs
