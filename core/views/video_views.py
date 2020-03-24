import urllib.parse as urlparse
from urllib.parse import parse_qs

from django.urls import reverse_lazy
from django.db.models import Q
from django.http import HttpResponse

from django.conf import settings

from django_filters import FilterSet

from haystack.query import SearchQuerySet

from dal import autocomplete

from django_tables2 import Column

from core.utils.generics import (
    CustomListView,
    CustomTable,
    CustomCreateView,
    CustomUpdateView,
    CustomDetailView,
    CustomDeleteView
)

from core.models.video import Video
from core.forms import VideoForm, DebaterForm
from core.utils.perms import has_perm

from taggit.models import Tag


class VideoFilter(FilterSet):
    class Meta:
        model = Video
        fields = {
            'id': ['exact'],
            'tournament__name': ['icontains'],
            'tournament__season': ['exact'],
            'round': ['exact']
        }


class VideoTable(CustomTable):
    id = Column(linkify=True)

    class Meta:
        model = Video
        fields = ('id',
                  'tournament',
                  'tournament.season',
                  'round',
                  'pm',
                  'mg',
                  'lo',
                  'mo',
                  'permissions')


class VideoListView(CustomListView):
    public_view = True
    model = Video
    table_class = VideoTable
    template_name = 'videos/list.html'

    filterset_class = VideoFilter

    buttons = [
        {
            'name': 'Create',
            'href': reverse_lazy('core:video_create'),
            'perm': 'core.add_video',
            'class': 'btn-success'
        }
    ]


class VideoCreateView(CustomCreateView):
    model = Video

    form_class = VideoForm
    template_name = 'videos/create.html'

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        context['debater_form'] = DebaterForm()

        return context


class VideoUpdateView(CustomUpdateView):
    model = Video

    form_class = VideoForm
    template_name = 'videos/update.html'


class VideoDeleteView(CustomDeleteView):
    model = Video
    success_url = reverse_lazy('core:video_list')

    template_name = 'videos/delete.html'


class VideoDetailView(CustomDetailView):
    def has_permission(self, *args, **kwargs):
        return has_perm(self.request.user,
                        self.get_object())

    public_view = True
    model = Video
    template_name = 'videos/detail.html'

    buttons = [
        {
            'name': 'Delete',
            'href': 'core:video_delete',
            'perm': 'core.delete_video',
            'class': 'btn-danger',
            'include_pk': True
        },
        {
            'name': 'Edit',
            'href': 'core:video_update',
            'perm': 'core.change_video',
            'class': 'btn-info',
            'include_pk': True
        },
    ]

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        embed = 'youtube' in self.object.link

        context['embed'] = embed

        if embed:
            context['embed_link'] = 'https://www.youtube.com/embed/%s' % (
                parse_qs(urlparse.urlparse(self.object.link).query)['v'][0],
            )

        return context


class TagAutocomplete(autocomplete.Select2QuerySetView):
    def get_queryset(self):
        qs = Tag.objects.all()

        if self.q:
            qs = qs.filter(name__istartswith=self.q)

        return qs
