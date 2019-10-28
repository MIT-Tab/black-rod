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
from core.models.standings.toty import TOTY

class TOTYFilter(FilterSet):
    class Meta:
        model = TOTY
        fields = {
            'team__id': ['exact'],
            'team__name': ['icontains'],
            'season': ['exact']
        }


class TOTYTable(CustomTable):
    marker_one = MarkerColumn(number='one', verbose_name='1')
    marker_two = MarkerColumn(number='two', verbose_name='2')
    marker_three = MarkerColumn(number='three', verbose_name='3')
    marker_four = MarkerColumn(number='four', verbose_name='4')
    marker_five = MarkerColumn(number='five', verbose_name='5')

    team = Column(verbose_name='Team', accessor='team.name', linkify=lambda record: record.team.get_absolute_url())

    class Meta:
        model = TOTY
        fields = ('place',
                  'team',
                  'team.debaters',
                  'points',
                  'marker_one',
                  'marker_two',
                  'marker_three',
                  'marker_four',
                  'marker_five')


class TOTYListView(CustomListView):
    public_view = True    
    model = TOTY
    table_class = TOTYTable
    template_name = 'totys/list.html'

    filterset_class = TOTYFilter

    def get(self, request, *args, **kwargs):
        if not self.request.GET.get('season'):
            return redirect(reverse('core:toty') + '?season=' + settings.CURRENT_SEASON)
        return super().get(request, *args, **kwargs)
