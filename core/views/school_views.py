from django.urls import reverse_lazy

from django_filters import FilterSet
from django_tables2 import Column

from core.utils.generics import (
    CustomListView,
    CustomTable,
    CustomCreateView,
    CustomUpdateView,
    CustomDetailView,
    CustomDeleteView
)
from core.models.school import School


class SchoolFilter(FilterSet):
    class Meta:
        model = School
        fields = {
            'id': ['exact'],
            'name': ['icontains'],
            'included_in_oty': ['exact'],
        }


class SchoolTable(CustomTable):
    id = Column(linkify=True)

    class Meta:
        model = School
        fields = ('id', 'name', 'included_in_oty')


class SchoolListView(CustomListView):
    model = School
    table_class = SchoolTable
    template_name = 'schools/list.html'

    filterset_class = SchoolFilter

    buttons = [
        {
            'name': 'Create',
            'href': reverse_lazy('core:school_create'),
            'perm': 'core.add_school',
            'class': 'btn-success'
        }
    ]


class SchoolDetailView(CustomDetailView):
    model = School
    template_name = 'schools/detail.html'

    buttons = [
        {
            'name': 'Delete',
            'href': 'core:school_delete',
            'perm': 'core.remove_school',
            'class': 'btn-danger',
            'include_pk': True
        },
        {
            'name': 'Edit',
            'href': 'core:school_update',
            'perm': 'core.change_school',
            'class': 'btn-info',
            'include_pk': True
        },
    ]


class SchoolUpdateView(CustomUpdateView):
    model = School

    fields = ['name', 'included_in_oty']
    template_name = 'schools/update.html'


class SchoolCreateView(CustomCreateView):
    model = School

    fields = ['name', 'included_in_oty']
    template_name = 'schools/create.html'


class SchoolDeleteView(CustomDeleteView):
    model = School
    success_url = reverse_lazy('core:school_list')

    template_name = 'schools/delete.html'
