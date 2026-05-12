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
