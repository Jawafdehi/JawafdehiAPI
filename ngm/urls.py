# DEPRECATED (Decision Q13): these routes are a thin REST pass-through to the
# standalone NGM API service (ngm/client.py), kept only while consumers migrate
# off the backend proxy. Responses carry a `Deprecation` header. See
# think-big/ngm/ngm-api-plane.md §4.
from django.urls import path

from ngm.api_views import CourtCaseDetailView, NGMJudicialQueryView

urlpatterns = [
    path(
        "ngm/query_judicial", NGMJudicialQueryView.as_view(), name="ngm-query-judicial"
    ),
    path(
        "ngm/court_case/<str:case_id>",
        CourtCaseDetailView.as_view(),
        name="ngm-court-case-detail",
    ),
]
