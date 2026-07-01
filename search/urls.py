"""URLs for the unified ``search`` app — mounted at ``/api/search/``."""

from django.urls import path

from .views import UnifiedSearchView

app_name = "search"

urlpatterns = [
    path("", UnifiedSearchView.as_view(), name="unified-search"),
]
