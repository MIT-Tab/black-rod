from django.views.generic import (
    CreateView,
    DetailView,
    UpdateView,
    DeleteView
)
from django.contrib.auth.mixins import PermissionRequiredMixin

import django_tables2 as tables
from django_filters.views import FilterView

from core.templatetags.tags import number


class CustomTable(tables.Table):
    class Meta:
        per_page = 10


class CustomMixin(PermissionRequiredMixin):
    buttons = []

    permission_type = ''

    public_view = False

    def has_permission(self, *args, **kwargs):
        if self.public_view:
            return True
        return super().has_permission(*args, **kwargs)

    def get_permission_required(self):
        if self.permission_required is None:
            self.permission_required = 'core.%s_%s' % (self.permission_type,
                                                       self.model._meta.model_name)

        return super().get_permission_required()

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        context['buttons'] = self.buttons

        return context

class CustomListView(CustomMixin, tables.SingleTableMixin, FilterView):
    permission_type = 'view'


class CustomCreateView(CustomMixin, CreateView):
    permission_type = 'add'


class CustomDetailView(CustomMixin, DetailView):
    permission_type = 'view'


class CustomUpdateView(CustomMixin, UpdateView):
    permission_type = 'change'


class CustomDeleteView(CustomMixin, DeleteView):
    permission_type = 'delete'


class MarkerColumn(tables.Column):
    number = ''

    def __init__(self, number, *args, **kwargs):
        self.number = number
        super().__init__(*args, **kwargs)
        
    def render(self, record):
        if getattr(record, 'marker_%s' % (self.number,)) == 0 or not getattr(record, 'tournament_%s' % (self.number,)):
            return ''
        return '%s (%s)' % (number(getattr(record, 'marker_%s' % (self.number,)), "-2"),
                            getattr(record, 'tournament_%s' % (self.number,)))


class PlaceColumn(tables.Column):
    def render(self, record):
        if record.tied:
            return 'T-%s' % (record.place,)
        return '%s' % (record.place,)


class PointsColumn(tables.Column):
    def render(self, record):
        return '%s' % (number(record.points),)

class SeasonColumn(tables.Column):
    def render(self, record):
        if hasattr(record, 'get_season_display'):
            return record.get_season_display()
        elif hasattr(record, 'tournament') and hasattr(record.tournament, 'get_season_display'):
            return record.tournament.get_season_display()
        else:
            return getattr(record, 'season', 'Unknown')
