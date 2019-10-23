from django.urls import path
from django.contrib import admin

from django.conf import settings

import core.views.views as views
import core.views.school_views as school_views
import core.views.debater_views as debater_views

app_name = 'core'

urlpatterns = [
    path('', views.index, name='index'),

    path('core/schools/',
         school_views.SchoolListView.as_view(),
         name='school_list'),
    path('core/schools/<int:pk>',
         school_views.SchoolDetailView.as_view(),
         name='school_detail'),
    path('core/schools/<int:pk>/edit',
         school_views.SchoolUpdateView.as_view(),
         name='school_update'),
    path('core/schools/<int:pk>/delete',
         school_views.SchoolDeleteView.as_view(),
         name='school_delete'),
    path('core/schools/create',
         school_views.SchoolCreateView.as_view(),
         name='school_create'),
    path('core/schools/autocomplete',
         school_views.SchoolAutocomplete.as_view(),
         name='school_autocomplete'),

    path('core/debaters/',
         debater_views.DebaterListView.as_view(),
         name='debater_list'),
    path('core/debaters/<int:pk>',
         debater_views.DebaterDetailView.as_view(),
         name='debater_detail'),
    path('core/debaters/<int:pk>/edit',
         debater_views.DebaterUpdateView.as_view(),
         name='debater_update'),
    path('core/debaters/<int:pk>/delete',
         debater_views.DebaterDeleteView.as_view(),
         name='debater_delete'),
    path('core/debaters/create',
         debater_views.DebaterCreateView.as_view(),
         name='debater_create')    
]
