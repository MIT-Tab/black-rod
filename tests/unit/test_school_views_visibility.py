import pytest
from django.http import Http404
from django.test import RequestFactory

from core.models import School
from core.views.school_views import SchoolDetailView, SchoolListView


@pytest.mark.django_db
def test_school_list_view_excludes_synthetic_schools():
    visible_school = School.objects.create(name="Visible School")
    hidden_school = School.all_objects.create(name="Synthetic School", synthetic=True)

    request = RequestFactory().get("/core/schools/")
    view = SchoolListView()
    view.setup(request)

    queryset = list(view.get_queryset())

    assert visible_school in queryset
    assert hidden_school not in queryset


@pytest.mark.django_db
def test_school_detail_view_404s_for_synthetic_school():
    synthetic_school = School.all_objects.create(name="Synthetic School", synthetic=True)

    request = RequestFactory().get(f"/core/schools/{synthetic_school.pk}?season=2024")
    view = SchoolDetailView()
    view.setup(request, pk=synthetic_school.pk)

    with pytest.raises(Http404):
        view.get_object()
