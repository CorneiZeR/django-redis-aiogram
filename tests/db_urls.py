"""The admin, so the database suite can drive it with a real test client."""

from django.contrib import admin
from django.urls import path

urlpatterns = [path('admin/', admin.site.urls)]
