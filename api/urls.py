"""
URL routing for API endpoints.
"""

from django.urls import path
from . import views

app_name = 'api'

urlpatterns = [
    path('schools/', views.ActiveSchoolListAPIView.as_view(), name='active_schools'),
    path('schools/all/', views.AllSchoolListAPIView.as_view(), name='all_schools'),
    path('debaters/<int:school_id>/', views.SchoolDebatersAPIView.as_view(), name='school_debaters'),
]
