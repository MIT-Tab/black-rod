from django.views.generic import (
    CreateView,
    DetailView,
    UpdateView,
    DeleteView
)

import django_tables2 as tables
from django_filters.views import FilterView


class CustomTable(tables.Table):
    class Meta:
        per_page = 10


class CustomMixin():
    buttons = []

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        context['buttons'] = self.buttons

        return context

class CustomListView(CustomMixin, tables.SingleTableMixin, FilterView):
    pass


class CustomCreateView(CustomMixin, CreateView):
    pass


class CustomDetailView(CustomMixin, DetailView):
    pass


class CustomUpdateView(CustomMixin, UpdateView):
    pass


class CustomDeleteView(CustomMixin, DeleteView):
    pass
