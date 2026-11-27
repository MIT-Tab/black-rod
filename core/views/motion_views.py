import random

from dal import autocomplete
from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core import signing
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, reverse
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import FormView
from django_filters import ChoiceFilter, DateFilter, FilterSet
from django_tables2 import Column

from core.forms import MotionForm, MotionImportForm
from core.models import Motion, MotionTopic, MotionUserStatus
from core.utils.filter import TagFilter
from core.utils.generics import CustomCreateView, CustomDetailView, CustomListView, CustomTable, CustomUpdateView
from core.utils.motion_import import import_motion_rows, read_motion_spreadsheet


class BestMatchTagFilter(TagFilter):
    def filter(self, qs, value):
        names = self._normalize(value) if value else []
        if not names:
            return qs
        return qs.annotate(
            topic_matches=Count(
                "tagged_motion_items",
                filter=Q(tagged_motion_items__tag__name__in=names),
                distinct=True,
            )
        ).filter(topic_matches__gt=0).order_by("-topic_matches", "-date_set", "-pk")


PROGRESS_CHOICES = (
    ("exclude_done_ignore", "Exclude done and ignored"),
    ("exclude_ignore", "Exclude ignored"),
    ("all", "Show all"),
    ("done", "Only done"),
    ("ignore", "Only ignored"),
)


class MotionFilter(FilterSet):
    tags_all = TagFilter(match="all", label="Contains all topics", widget=autocomplete.TaggitSelect2("core:motion_topic_autocomplete_no_create"))
    topics = BestMatchTagFilter(label="Best match for topics", widget=autocomplete.TaggitSelect2("core:motion_topic_autocomplete_no_create"))
    date_from = DateFilter(
        field_name="date_set",
        lookup_expr="gte",
        label="Date set from",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    date_to = DateFilter(
        field_name="date_set",
        lookup_expr="lte",
        label="Date set through",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    progress = ChoiceFilter(
        field_name="id",
        label="Progress",
        choices=PROGRESS_CHOICES,
        method="filter_progress",
        empty_label=None,
        initial="exclude_done_ignore",
    )

    class Meta:
        model = Motion
        fields = {"text": ["icontains"], "tournament__name": ["icontains"]}

    def filter_progress(self, queryset, name, value):
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated or value == "all":
            return queryset
        statuses = MotionUserStatus.objects.filter(user=user, motion=OuterRef("pk"))
        if value == "exclude_done_ignore":
            return queryset.annotate(has_user_status=Exists(statuses)).filter(has_user_status=False)
        if value == "exclude_ignore":
            return queryset.annotate(is_ignored=Exists(statuses.filter(status=MotionUserStatus.IGNORE))).filter(is_ignored=False)
        return queryset.filter(user_statuses__user=user, user_statuses__status=value)


class MotionTable(CustomTable):
    text = Column(linkify=True)
    topics = Column(empty_values=(), orderable=False)
    user_status = Column(empty_values=(), orderable=False, verbose_name="My status")

    def render_topics(self, record):
        return ", ".join(tag.name for tag in record.tags.all())

    def render_user_status(self, record):
        return getattr(record, "user_status", "") or "—"

    class Meta:
        model = Motion
        fields = ("text", "date_set", "tournament", "topics", "user_status")


def motion_queryset_for_request(request):
    queryset = Motion.objects.select_related("tournament").prefetch_related("tags")
    if request.user.is_authenticated:
        status = MotionUserStatus.objects.filter(user=request.user, motion=OuterRef("pk")).values("status")[:1]
        queryset = queryset.annotate(user_status=Subquery(status))
    return queryset


class MotionListView(CustomListView):
    public_view = True
    model = Motion
    table_class = MotionTable
    template_name = "motions/list.html"
    filterset_class = MotionFilter
    ordering = None
    buttons = [
        {"name": "Create", "href": reverse_lazy("core:motion_create"), "perm": "core.add_motion", "class": "btn-success"},
        {"name": "Bulk import", "href": reverse_lazy("core:motion_import"), "perm": "core.add_motion", "class": "btn-info"},
    ]

    def get_queryset(self):
        return motion_queryset_for_request(self.request)

    def get_filterset_kwargs(self, filterset_class):
        kwargs = super().get_filterset_kwargs(filterset_class)
        data = self.request.GET.copy()
        if "progress" not in data:
            data["progress"] = "exclude_done_ignore"
        kwargs["data"] = data
        return kwargs


class MotionRandomView(View):
    def get(self, request):
        data = request.GET.copy()
        if "progress" not in data:
            data["progress"] = "exclude_done_ignore"
        queryset = MotionFilter(data, queryset=motion_queryset_for_request(request), request=request).qs.distinct()
        ids = list(queryset.values_list("pk", flat=True))
        if not ids:
            messages.warning(request, "No motions match those filters.")
            return redirect(f"{reverse('core:motion_list')}?{data.urlencode()}")
        return redirect("core:motion_detail", pk=random.choice(ids))


class MotionTopicAutocomplete(autocomplete.Select2QuerySetView):
    def get_queryset(self):
        queryset = MotionTopic.objects.all()
        if self.q:
            queryset = queryset.filter(name__istartswith=self.q)
        return queryset


class MotionCreateView(CustomCreateView):
    model = Motion
    form_class = MotionForm
    template_name = "motions/form.html"


class MotionUpdateView(CustomUpdateView):
    model = Motion
    form_class = MotionForm
    template_name = "motions/form.html"


class MotionDetailView(CustomDetailView):
    public_view = True
    model = Motion
    template_name = "motions/detail.html"
    buttons = [{"name": "Edit", "href": "core:motion_update", "perm": "core.change_motion", "class": "btn-info", "include_pk": True}]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            context["motion_status"] = MotionUserStatus.objects.filter(user=self.request.user, motion=self.object).values_list("status", flat=True).first()
        return context


class MotionStatusView(LoginRequiredMixin, View):
    def post(self, request, pk):
        motion = get_object_or_404(Motion, pk=pk)
        status = request.POST.get("status")
        if status in dict(MotionUserStatus.STATUS_CHOICES):
            MotionUserStatus.objects.update_or_create(user=request.user, motion=motion, defaults={"status": status})
        elif status == "clear":
            MotionUserStatus.objects.filter(user=request.user, motion_id=pk).delete()
        else:
            return HttpResponseBadRequest("Invalid status")
        return redirect(request.POST.get("next") or reverse("core:motion_detail", kwargs={"pk": pk}))


class MotionImportView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    form_class = MotionImportForm
    template_name = "motions/import.html"

    def test_func(self):
        return self.request.user.has_perm("core.add_motion")

    def form_valid(self, form):
        try:
            rows = read_motion_spreadsheet(form.cleaned_data["spreadsheet"])
        except (ValueError, UnicodeDecodeError) as exc:
            form.add_error("spreadsheet", str(exc))
            return self.form_invalid(form)
        return self.render_to_response(self.get_context_data(form=form, rows=rows, signed_rows=signing.dumps(rows, compress=True)))

    def post(self, request, *args, **kwargs):
        if request.POST.get("confirm"):
            try:
                rows = signing.loads(request.POST.get("rows", ""), max_age=3600)
            except signing.BadSignature:
                return HttpResponseBadRequest("Import preview expired or was changed.")
            created, skipped = import_motion_rows(rows)
            messages.success(request, f"Imported {created} motions; skipped {skipped} invalid or duplicate rows.")
            return redirect("core:motion_list")
        return super().post(request, *args, **kwargs)
