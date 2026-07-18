"""URLs for the unified ``search`` app — mounted at ``/api/search/``."""

from django.urls import path

from .views import SearchClickView, UnifiedSearchView

app_name = "search"

urlpatterns = [
    path("", UnifiedSearchView.as_view(), name="unified-search"),
    # No trailing slash: the SPA beacons to exactly /api/search/click, and
    # APPEND_SLASH cannot redirect a POST (it would error instead of redirecting).
    path("click", SearchClickView.as_view(), name="search-click"),
]
