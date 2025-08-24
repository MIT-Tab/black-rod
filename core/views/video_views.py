import urllib.parse as urlparse
from urllib.parse import parse_qs

from dal import autocomplete
from django.conf import settings
from django.shortcuts import redirect, reverse
from django.urls import reverse_lazy
from django.views import View
from django_filters import ChoiceFilter, FilterSet
from django_tables2 import Column
from taggit.models import Tag

from core.forms import DebaterForm, VideoForm
from core.models.video import Video
from core.utils.filter import TagFilter
from core.utils.generics import (
    CustomCreateView,
    CustomDeleteView,
    CustomDetailView,
    CustomListView,
    CustomTable,
    CustomUpdateView,
    SeasonColumn,
)
from core.utils.perms import has_perm


class VideoFilter(FilterSet):
    tags = TagFilter(
        field_name="tags__name",
        widget=autocomplete.TaggitSelect2("core:tag_autocomplete_no_create"),
    )
    tournament__season = ChoiceFilter(
        choices=settings.SEASONS, empty_label="Any Season", label="Season"
    )

    class Meta:
        model = Video
        fields = {
            "id": ["exact"],
            "tournament__name": ["icontains"],
            "round": ["exact"],
        }


class VideoTable(CustomTable):
    id = Column(linkify=True)
    tournament_season = SeasonColumn(
        verbose_name="Season",
        accessor="tournament__season",
        order_by="tournament__season",
    )

    class Meta:
        model = Video
        fields = (
            "id",
            "tournament",
            "tournament_season",
            "round",
            "pm",
            "mg",
            "lo",
            "mo",
            "permissions",
        )


class VideoListView(CustomListView):
    public_view = True
    model = Video
    table_class = VideoTable
    template_name = "videos/list.html"

    filterset_class = VideoFilter

    buttons = [
        {
            "name": "Create",
            "href": reverse_lazy("core:video_create"),
            "perm": "core.add_video",
            "class": "btn-success",
        }
    ]


class VideoCreateView(CustomCreateView):
    model = Video

    form_class = VideoForm
    template_name = "videos/create.html"

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        context["debater_form"] = DebaterForm()

        return context


class VideoUpdateView(CustomUpdateView):
    model = Video

    form_class = VideoForm
    template_name = "videos/update.html"


class VideoDeleteView(CustomDeleteView):
    model = Video
    success_url = reverse_lazy("core:video_list")

    template_name = "videos/delete.html"


class VideoDetailView(CustomDetailView):
    def has_permission(self, *args, **kwargs):
        return has_perm(self.request.user, self.get_object())

    public_view = True
    model = Video
    template_name = "videos/detail.html"

    buttons = [
        {
            "name": "Delete",
            "href": "core:video_delete",
            "perm": "core.delete_video",
            "class": "btn-danger",
            "include_pk": True,
        },
        {
            "name": "Edit",
            "href": "core:video_update",
            "perm": "core.change_video",
            "class": "btn-info",
            "include_pk": True,
        },
    ]

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)

        youtube_embed = (
            "youtube" in self.object.link and "playlist" not in self.object.link
        )
        vimeo_embed = "vimeo" in self.object.link and self.object.password == ""

        context["embed"] = youtube_embed or vimeo_embed

        if youtube_embed:
            context["embed_link"] = (
                f"https://www.youtube.com/embed/{parse_qs(urlparse.urlparse(self.object.link).query)['v'][0]}"
            )
        elif vimeo_embed:
            context["embed_link"] = (
                f"https://player.vimeo.com/video/{self.object.link.split('/')[-1]}"
            )

        return context


class TagAutocomplete(autocomplete.Select2QuerySetView):
    def get_queryset(self):
        qs = Tag.objects.all()

        if self.q:
            qs = qs.filter(name__istartswith=self.q)

        return qs


class TagDetail(View):
    def get(self, request, *args, **kwargs):
        return redirect(reverse("core:video_list") + "?tags=" + kwargs["slug"])
