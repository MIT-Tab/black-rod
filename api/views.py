from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q
from django.http import JsonResponse, Http404
from django.views import View

from core.models.school import School
from core.models.debater import Debater
from .serializers import serialize_school, serialize_debater


class ActiveSchoolListAPIView(View):
    """
    API endpoint to list schools with recent activity.

    GET /api/schools/
    Returns: Top 25 schools by number of active debaters in last 2 years
    Cached for 5 minutes.
    """

    def get(self, request):
        cache_key = 'api:active_schools'
        cached_data = cache.get(cache_key)

        if cached_data is not None:
            return JsonResponse(cached_data)

        current_year = int(settings.CURRENT_SEASON)
        cutoff_season = str(current_year - 2)

        schools = School.objects.filter(
            debaters__latest_season__gte=cutoff_season
        ).annotate(
            active_debater_count=Count(
                'debaters',
                filter=Q(debaters__latest_season__gte=cutoff_season)
            )
        ).order_by('-active_debater_count', 'name')[:25]

        data = {
            "count": len(schools),
            "schools": [serialize_school(school) for school in schools]
        }

        cache.set(cache_key, data, 300)

        return JsonResponse(data)


class AllSchoolListAPIView(View):
    """
    API endpoint to list all schools.

    GET /api/schools/all/
    Returns: List of all schools in the database
    Cached for 5 minutes.
    """

    def get(self, request):
        cache_key = 'api:all_schools'
        cached_data = cache.get(cache_key)

        if cached_data is not None:
            return JsonResponse(cached_data)

        schools = School.objects.all().order_by('name')

        data = {
            "count": schools.count(),
            "schools": [serialize_school(school) for school in schools]
        }
        cache.set(cache_key, data, 300)

        return JsonResponse(data)


class SchoolDebatersAPIView(View):
    """
    API endpoint to list debaters from a specific school.

    GET /api/debaters/<school_id>/
    Returns: List of debaters active in the last 5 years for the specified school
    Cached for 5 minutes per school.
    """

    def get(self, request, school_id):
        cache_key = f'api:school_debaters:{school_id}'
        cached_data = cache.get(cache_key)

        if cached_data is not None:
            return JsonResponse(cached_data)

        try:
            school = School.objects.get(id=school_id)
        except School.DoesNotExist as exc:
            raise Http404("School not found") from exc

        current_year = int(settings.CURRENT_SEASON)
        cutoff_year = current_year - 5
        cutoff_season = str(cutoff_year)

        debaters = Debater.objects.filter(
            school=school,
            latest_season__gte=cutoff_season
        ).select_related('school').order_by('last_name', 'first_name')

        data = {
            "school": serialize_school(school),
            "count": debaters.count(),
            "debaters": [serialize_debater(debater) for debater in debaters]
        }

        cache.set(cache_key, data, 300)

        return JsonResponse(data)
