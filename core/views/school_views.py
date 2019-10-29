from django.urls import reverse_lazy
from django.shortcuts import reverse, redirect

from django.conf import settings

from django_filters import FilterSet

from django_tables2 import Column

from dal import autocomplete

from core.utils.generics import (
    CustomListView,
    CustomTable,
    CustomCreateView,
    CustomUpdateView,
    CustomDetailView,
    CustomDeleteView
)
from core.models.school import School
from core.models.debater import QualPoints


class SchoolFilter(FilterSet):
    class Meta:
        model = School
        fields = {
            'id': ['exact'],
            'name': ['icontains'],
        }


class SchoolTable(CustomTable):
    id = Column(linkify=True)
    included_in_oty = Column(verbose_name='APDA Member?')

    class Meta:
        model = School
        fields = ('id', 'name', 'included_in_oty')


class SchoolListView(CustomListView):
    public_view = True    
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
    public_view = True    
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

    def get(self, request, *args, **kwargs):
        if not 'season' in self.request.GET:
            return redirect(
                reverse('core:school_detail',
                        kwargs={'pk': self.get_object().id}) + '?season=%s' % (settings.CURRENT_SEASON,))
        return super().get(request, *args, **kwargs)

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        context['cotys'] = self.object.coty.order_by(
            '-season'
        )

        context['debaters'] = QualPoints.objects.filter(
            debater__school=self.object
        ).filter(
            season=self.request.GET.get('season')
        ).filter(
            points__gt=0
        ).order_by(
            '-points'
        )

        context['seasons'] = settings.SEASONS
        context['current_season'] = self.request.GET.get('season')

        return context


class SchoolUpdateView(CustomUpdateView):
    model = School

    fields = ['name', 'included_in_oty']
    template_name = 'schools/update.html'

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        context['cotys'] = self.object.coty.order_by(
            '-season'
        )

        return context


class SchoolCreateView(CustomCreateView):
    model = School

    fields = ['name', 'included_in_oty']
    template_name = 'schools/create.html'


class SchoolDeleteView(CustomDeleteView):
    model = School
    success_url = reverse_lazy('core:school_list')

    template_name = 'schools/delete.html'


class SchoolAutocomplete(autocomplete.Select2QuerySetView):
    def get_result_label(self, record):
        return '<%s> %s' % (record.id,
                            record.name)
    
    def get_queryset(self):
        qs = School.objects.all()

        if self.q:
            qs = qs.filter(name__icontains=self.q)

        return qs
