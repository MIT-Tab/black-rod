from django.contrib import admin
from django.urls import include, path
from django.views.generic.base import RedirectView
from api.views import LLMProxyView

favicon_view = RedirectView.as_view(url='/favicon.ico', permanent=True)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("auth/", include("django.contrib.auth.urls")),
    path("accounts/", include("allauth.urls")),
    path("search/", include("haystack.urls")),
    path("summernote/", include("django_summernote.urls")),
    path("llm/", LLMProxyView.as_view(), name='llm_proxy'),
    path("api/", include("api.urls")),
    path("", include("core.urls")),
]
