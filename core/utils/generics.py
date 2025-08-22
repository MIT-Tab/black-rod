import django_tables2 as tables
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.views.generic import CreateView, DeleteView, DetailView, UpdateView
from django_filters.views import FilterView

from core.templatetags.tags import number


class CustomTable(tables.Table):
    class Meta:
        per_page = 10


class CustomMixin(PermissionRequiredMixin):
    buttons = []

    permission_type = ""

    public_view = False

    def has_permission(self, *args, **kwargs):
        if self.public_view:
            return True
        return super().has_permission(*args, **kwargs)

    def get_permission_required(self):
        if self.permission_required is None:
            self.permission_required = (
                f"core.{self.permission_type}_{self.model._meta.model_name}"
            )

        return super().get_permission_required()

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        context["buttons"] = self.buttons

        return context


class CustomListView(CustomMixin, tables.SingleTableMixin, FilterView):
    permission_type = "view"
    ordering = ["-pk"]


class CustomCreateView(CustomMixin, CreateView):
    permission_type = "add"


class CustomDetailView(CustomMixin, DetailView):
    permission_type = "view"


class CustomUpdateView(CustomMixin, UpdateView):
    permission_type = "change"


class CustomDeleteView(CustomMixin, DeleteView):
    permission_type = "delete"


class MarkerColumn(tables.Column):
    number = ""

    def __init__(self, number, *args, **kwargs):
        self.number = number
        super().__init__(*args, **kwargs)

    def render(self, record):
        if getattr(record, f"marker_{self.number}") == 0 or not getattr(
            record, f"tournament_{self.number}"
        ):
            return ""
        return f"{number(getattr(record, f'marker_{self.number}'), '-2')} ({getattr(record, f'tournament_{self.number}')})"


class PlaceColumn(tables.Column):
    def render(self, record):
        if record.tied:
            return f"T-{record.place}"
        return f"{record.place}"


class PointsColumn(tables.Column):
    def render(self, record):
        return f"{number(record.points)}"


class SeasonColumn(tables.Column):
    def render(self, record):
        if hasattr(record, "get_season_display"):
            return record.get_season_display()
        if hasattr(record, "tournament") and hasattr(
            record.tournament, "get_season_display"
        ):
            return record.tournament.get_season_display()

        return getattr(record, "season", "Unknown")
