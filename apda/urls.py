from django.urls import path, include
from django.contrib import admin

urlpatterns = [
    path('admin/', admin.site.urls),

    path('auth/', include('django.contrib.auth.urls')),
    path('accounts/', include('allauth.urls')),

    path('search/', include('haystack.urls')),

    path('summernote/', include('django_summernote.urls')),

    path('', include('core.urls'))
]
