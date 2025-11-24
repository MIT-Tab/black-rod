"""
URL routing for API endpoints.
"""

from django.urls import path
from . import views

app_name = 'api'

urlpatterns = [
    path('standings/', views.SeasonStandingsAPIView.as_view(), name='season_standings'),
    path('standings/replay/', views.SeasonStandingsReplayAPIView.as_view(), name='season_standings_replay'),
    path('standings/through-date/', views.StandingsThroughDateAPIView.as_view(), name='standings_through_date'),
    path('schedule/', views.ScheduleAPIView.as_view(), name='schedule'),
    path('schools/', views.ActiveSchoolListAPIView.as_view(), name='active_schools'),
    path('schools/all/', views.AllSchoolListAPIView.as_view(), name='all_schools'),
    path('schools/<int:pk>/detail/', views.SchoolDetailAPIView.as_view(), name='school_detail'),
    path('debaters/<int:school_id>/', views.SchoolDebatersAPIView.as_view(), name='school_debaters'),
    path('debaters/<int:pk>/detail/', views.DebaterDetailAPIView.as_view(), name='debater_detail'),
    path('teams/<int:pk>/detail/', views.TeamDetailAPIView.as_view(), name='team_detail'),
    path('tournaments/<int:pk>/detail/', views.TournamentDetailAPIView.as_view(), name='tournament_detail'),
    path('oty-guide/', views.OTYGuideAPIView.as_view(), name='oty_guide'),
]
