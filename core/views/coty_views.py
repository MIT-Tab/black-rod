import itertools

from django.shortcuts import redirect, reverse
from django.conf import settings

from django_filters import FilterSet

from django_tables2 import Column

from core.utils.generics import (
    CustomTable,
    CustomListView,
    MarkerColumn
)
from core.models.standings.coty import COTY

class COTYFilter(FilterSet):
    class Meta:
        model = COTY
        fields = {
            'school__id': ['exact'],
            'school__name': ['icontains'],
            'season': ['exact']
        }


class COTYTable(CustomTable):
    school = Column(verbose_name='School', accessor='school.name', linkify=lambda record: record.school.get_absolute_url())
    
    class Meta:
        model = COTY
        fields = ('place',
                  'school',
                  'points')



class COTYListView(CustomListView):
    public_view = True    
    model = COTY
    table_class = COTYTable
    template_name = 'cotys/list.html'

    filterset_class = COTYFilter

    def get(self, request, *args, **kwargs):
        if not self.request.GET.get('season'):
            return redirect(reverse('core:coty') + '?season=' + settings.CURRENT_SEASON)
        return super().get(request, *args, **kwargs)
