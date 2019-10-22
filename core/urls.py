from django.urls import path
from django.contrib import admin

from django.conf import settings

import core.views.views as views
import core.views.school_views as school_views

app_name = 'core'

urlpatterns = [
    path('', views.index, name='index'),

    path('core/schools', school_views.SchoolListView.as_view(), name='school_list'),
    path('core/schools/<int:pk>', school_views.SchoolDetailView.as_view(), name='school_detail'),
    path('core/schools/<int:pk>/edit', school_views.SchoolUpdateView.as_view(), name='school_update'),
    path('core/schools/<int:pk>/delete', school_views.SchoolDeleteView.as_view(), name='school_delete'),
    path('core/schools/create', school_views.SchoolCreateView.as_view(), name='school_create')
]
