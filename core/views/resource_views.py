from dal import autocomplete
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.db.models import Q
from django.shortcuts import redirect, reverse
from django.urls import reverse_lazy
from django.views import View
from django_filters import ChoiceFilter, FilterSet
from django_tables2 import Column
from taggit.models import Tag
from django.utils.html import format_html

from core.forms import ResourceForm
from core.models.resource import Resource
from core.models.tags import ResourceTag
from core.utils.filter import TagFilter
from core.utils.generics import (
    CustomCreateView,
    CustomDeleteView,
    CustomDetailView,
    CustomListView,
    CustomTable,
    CustomUpdateView,
)


class ResourceFilter(FilterSet):
    tags_all = TagFilter(
        match="all",
        label="Contains all of these tags",
        widget=autocomplete.TaggitSelect2("core:resource_tag_autocomplete_no_create"),
    )
    tags = TagFilter(
        label="Contains any of these tags",
        widget=autocomplete.TaggitSelect2("core:resource_tag_autocomplete_no_create"),
    )
    resource_type = ChoiceFilter(
        choices=Resource.TYPE_CHOICES,
        empty_label="Any Type",
        label="Type"
    )

    class Meta:
        model = Resource
        fields = {
            "id": ["exact"],
            "title": ["icontains"],
            "authors__first_name": ["icontains"],
            "authors__last_name": ["icontains"],
        }


class ResourceTable(CustomTable):
    title = Column(linkify=True)
    authors = Column(accessor='authors', verbose_name='Authors', orderable=True, order_by='authors__last_name')
    tags = Column(accessor='tags', verbose_name='Tags', orderable=False)
    resource_type = Column(verbose_name='Type', attrs={'td': {'style': 'white-space: nowrap;'}})
    created_at = Column(verbose_name='Created', attrs={'td': {'style': 'white-space: nowrap;'}})

    def render_authors(self, record):
        author_links = []
        for author in record.authors.all():
            author_links.append(format_html(
                '<a href="{}">{}</a>',
                author.get_absolute_url(),
                author.name
            ))
        return format_html(", ".join(author_links))

    def render_tags(self, record):
        tag_links = []
        for tag in record.tags.all():
            # Get current query params and add this tag to the tags filter
            from django.http import QueryDict
            query_params = self.context['request'].GET.copy()
            
            # Add tag to existing tags filter (comma-separated)
            existing_tags = query_params.get('tags', '')
            if existing_tags:
                new_tags = f"{existing_tags},{tag.slug}"
            else:
                new_tags = tag.slug
            query_params['tags'] = new_tags
            
            tag_links.append(format_html(
                '<a href="?{}">{}</a>',
                query_params.urlencode(),
                tag.name
            ))
        return format_html(", ".join(tag_links))

    def render_resource_type(self, record):
        # Get current query params and set the resource_type filter
        from django.http import QueryDict
        query_params = self.context['request'].GET.copy()
        query_params['resource_type'] = record.resource_type
        
        return format_html(
            '<a href="?{}">{}</a>',
            query_params.urlencode(),
            record.get_resource_type_display()
        )

    def render_created_at(self, value):
        return value.strftime('%B %d, %Y')

    class Meta:
        model = Resource
        fields = (
            "title",
            "authors",
            "resource_type",
            "tags",
            "created_at",
        )


class ResourceListView(CustomListView):
    public_view = True
    model = Resource
    table_class = ResourceTable
    template_name = "resources/list.html"

    filterset_class = ResourceFilter

    buttons = [
        {
            "name": "Create",
            "href": reverse_lazy("core:resource_create"),
            "perm": "core.add_resource",
            "class": "btn-success",
        }
    ]

    def get_queryset(self):
        """Filter resources based on viewing permissions."""
        queryset = super().get_queryset()
        
        if self.request.user.is_superuser:
            return queryset
        
        if self.request.user.is_authenticated and self.request.user.can_view_private_videos:
            # User can see all resources
            return queryset
        elif self.request.user.is_authenticated:
            # Show public resources and resources they authored
            return queryset.filter(
                Q(viewing_permission=Resource.PUBLIC) |
                Q(authors__user=self.request.user)
            ).distinct()
        else:
            # Anonymous users only see public resources
            return queryset.filter(viewing_permission=Resource.PUBLIC)


class ResourceCreateView(LoginRequiredMixin, CustomCreateView):
    model = Resource
    form_class = ResourceForm
    template_name = "resources/create.html"

    def has_permission(self):
        """Allow any logged-in user with a claimed debater profile to create resources."""
        if self.request.user.is_superuser:
            return True
        return self.request.user.claimed_debaters.exists()

    def dispatch(self, request, *args, **kwargs):
        # Check if user has claimed debater profiles
        if not request.user.is_superuser and not request.user.claimed_debaters.exists():
            messages.error(request, "You must first claim a debater profile to post a resource.")
            return redirect('core:claim_debater_request_create')
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        # Set the creator
        form.instance.created_by = self.request.user
        return super().form_valid(form)


class MyResourcesView(LoginRequiredMixin, CustomListView):
    model = Resource
    table_class = ResourceTable
    template_name = "resources/my_resources.html"
    filterset_class = ResourceFilter

    def get_queryset(self):
        """Show only resources where the user is an author."""
        # Get all debater profiles claimed by the user
        user_debaters = self.request.user.claimed_debaters.all()
        # Get resources where any of those debaters are authors
        return Resource.objects.filter(authors__in=user_debaters).distinct()


class ResourceUpdateView(UserPassesTestMixin, CustomUpdateView):
    model = Resource
    form_class = ResourceForm
    template_name = "resources/update.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def test_func(self):
        """Only allow authors or superusers to edit."""
        resource = self.get_object()
        return (
            self.request.user.is_superuser or
            resource.is_author(self.request.user)
        )


class ResourceDeleteView(UserPassesTestMixin, CustomDeleteView):
    model = Resource
    success_url = reverse_lazy("core:resource_list")
    template_name = "resources/delete.html"

    def test_func(self):
        """Only allow authors or superusers to delete."""
        resource = self.get_object()
        return (
            self.request.user.is_superuser or
            resource.is_author(self.request.user)
        )


class ResourceDetailView(CustomDetailView):
    public_view = True
    model = Resource
    template_name = "resources/detail.html"

    def has_permission(self, *args, **kwargs):
        """Check if user has permission to view this resource."""
        resource = self.get_object()
        
        # Superusers can see everything
        if self.request.user.is_superuser:
            return True
        
        # Public resources are visible to everyone
        if resource.viewing_permission == Resource.PUBLIC:
            return True
        
        # For private resources, check if user is logged in
        if resource.viewing_permission == Resource.REQUIRES_LOGIN:
            if not self.request.user.is_authenticated:
                return False
            # Use same permission as videos
            if self.request.user.can_view_private_videos:
                return True
            # Authors can always see their own resources
            if resource.is_author(self.request.user):
                return True
        
        return False

    buttons = [
        {
            "name": "Delete",
            "href": "core:resource_delete",
            "perm": "core.delete_resource",
            "class": "btn-danger",
            "include_pk": True,
        },
        {
            "name": "Edit",
            "href": "core:resource_update",
            "perm": "core.change_resource",
            "class": "btn-info",
            "include_pk": True,
        },
    ]

    def get_context_data(self, *args, **kwargs):
        context = super().get_context_data(*args, **kwargs)
        
        # Check if user is an author
        context["is_author"] = self.object.is_author(self.request.user)
        
        return context


class ResourceTagAutocomplete(autocomplete.Select2QuerySetView):
    def get_queryset(self):
        qs = ResourceTag.objects.all()

        if self.q:
            qs = qs.filter(name__istartswith=self.q)

        return qs


class ResourceTagDetail(View):
    def get(self, request, *args, **kwargs):
        return redirect(reverse("core:resource_list") + "?tags=" + kwargs["slug"])
