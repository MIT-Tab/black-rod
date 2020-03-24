from django.urls import path
from django.contrib import admin

from django.conf import settings

import core.views.views as views
import core.views.school_views as school_views
import core.views.debater_views as debater_views
import core.views.tournament_views as tournament_views
import core.views.team_views as team_views
import core.views.round_views as round_views
import core.views.video_views as video_views

import core.views.soty_views as soty_views
import core.views.toty_views as toty_views
import core.views.noty_views as noty_views
import core.views.coty_views as coty_views

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
         name='debater_create'),
    path('core/debaters/autocomplete',
         debater_views.DebaterAutocomplete.as_view(),
         name='debater_autocomplete'),

    path('core/teams/',
         team_views.TeamListView.as_view(),
         name='team_list'),
    path('core/teams/<int:pk>',
         team_views.TeamDetailView.as_view(),
         name='team_detail'),
    path('core/teams/<int:pk>/edit',
         team_views.TeamUpdateView.as_view(),
         name='team_update'),
    path('core/teams/<int:pk>/delete',
         team_views.TeamDeleteView.as_view(),
         name='team_delete'),
    path('core/teams/autocomplete',
         team_views.TeamAutocomplete.as_view(),
         name='team_autocomplete'),

    path('core/videos/',
         video_views.VideoListView.as_view(),
         name='video_list'),
    path('core/videos/<int:pk>',
         video_views.VideoDetailView.as_view(),
         name='video_detail'),
    path('core/videos/<int:pk>/edit',
         video_views.VideoUpdateView.as_view(),
         name='video_update'),
    path('core/videos/<int:pk>/delete',
         video_views.VideoDeleteView.as_view(),
         name='video_delete'),
    path('core/videos/create',
         video_views.VideoCreateView.as_view(),
         name='video_create'),
    path('core/videos/tags',
         video_views.TagAutocomplete.as_view(create_field='name'),
         name='tag_autocomplete'),

    path('core/tournaments/',
         tournament_views.TournamentListView.as_view(),
         name='tournament_list'),
    path('core/schedules/',
         tournament_views.ScheduleView.as_view(),
         name='schedule_view'),
    path('core/tournaments/<int:pk>',
         tournament_views.TournamentDetailView.as_view(),
         name='tournament_detail'),
    path('core/tournaments/<int:pk>/edit',
         tournament_views.TournamentUpdateView.as_view(),
         name='tournament_update'),
    path('core/tournaments/<int:pk>/delete',
         tournament_views.TournamentDeleteView.as_view(),
         name='tournament_delete'),
    path('core/tournaments/create',
         tournament_views.TournamentCreateView.as_view(),
         name='tournament_create'),
    path('core/tournaments/autocomplete',
         tournament_views.TournamentAutocomplete.as_view(),
         name='tournament_autocomplete'),
    path('core/tournaments/all_autocomplete',
         tournament_views.AllTournamentAutocomplete.as_view(),
         name='all_tournament_autocomplete'),
    path('core/tournaments/data_entry',
         tournament_views.TournamentDataEntryWizardView.as_view(),
         name='tournament_dataentry'),
    path('core/tournaments/data_import',
         tournament_views.TournamentImportWizardView.as_view(),
         name='tournament_import'),

    path('core/rounds/<int:pk>',
         round_views.RoundDetailView.as_view(),
         name='round_detail'),

    path('core/soty',
         soty_views.SOTYListView.as_view(),
         name='soty'),
    path('core/toty',
         toty_views.TOTYListView.as_view(),
         name='toty'),
    path('core/noty',
         noty_views.NOTYListView.as_view(),
         name='noty'),
    path('core/coty',
         coty_views.COTYListView.as_view(),
         name='coty')
]
