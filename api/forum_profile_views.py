"""Minimal server-to-server lookup for linked WordPress forum profiles."""
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Debater


@method_decorator(never_cache, name="dispatch")
class ForumProfilesAPIView(APIView):
    """Resolve approved claims using the provider's immutable WordPress ID."""

    authentication_classes = []
    permission_classes = []

    @extend_schema(exclude=True)
    def post(self, request):
        """Return public profile fields only to the configured forum server."""
        token = settings.FORUM_PROFILE_API_TOKEN
        if not token or not constant_time_compare(
            request.headers.get("Authorization", ""), f"Bearer {token}"
        ):
            return Response({"detail": "Unauthorized"}, status=401)
        wordpress_id = request.data.get("wordpress_user_id") if isinstance(request.data, dict) else None
        if isinstance(wordpress_id, bool) or not isinstance(wordpress_id, int) or wordpress_id <= 0:
            return Response({"detail": "wordpress_user_id must be a positive integer"}, status=400)
        users = SocialAccount.objects.filter(
            provider="apdaonline", uid=str(wordpress_id), user__is_active=True
        ).values("user_id")
        debaters = Debater.objects.filter(user_id__in=users).select_related("school").order_by(
            "last_name", "first_name", "school__name", "pk"
        )
        return Response({"debaters": [
            {"id": debater.pk, "name": debater.name,
             "school": debater.school.name if debater.school else None}
            for debater in debaters
        ]})
