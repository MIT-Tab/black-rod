from django.urls import path
from django.contrib import admin

from django.conf import settings

from core import views

app_name = 'core'

urlpatterns = [
    path('', views.index, name='index')
]
