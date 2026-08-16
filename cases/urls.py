"""
URL configuration for the cases app API.

See: .kiro/specs/accountability-platform-core/design.md
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .api_views import (
    AuthorProfileView,
    CaseAuthorCandidateView,
    CaseViewSet,
    FeedbackTriageViewSet,
    FeedbackView,
    OEmbedView,
    StatisticsView,
)

# Create a router and register our viewsets.
#
# NOTE: there is no /api/entities/ route anymore. Entities are owned by the
# Nepal Entity Service (NES); Jawafdehi only stores the NES entity id on its
# case/source binds and resolves display details from NES. The former
# JawafEntity-backed entities endpoint was removed with the JawafEntity model.
router = DefaultRouter()
router.register(r"cases", CaseViewSet, basename="case")
# Staff read/triage queue. Registered on a path of its own rather than as a GET
# on ``feedback/`` because that route is the public, deliberately
# unauthenticated submission endpoint (see FeedbackTriageViewSet's docstring).
router.register(
    r"feedback-submissions", FeedbackTriageViewSet, basename="feedback-submission"
)

urlpatterns = [
    # NOTE: ``search/`` is no longer mounted here. The platform-wide unified
    # search moved to the ``search`` app (``GET /api/search/``, see
    # config/urls.py + search), replacing this cases-scoped
    # in-process search in the OpenSearch cutover (plan decision #5).
    # Roster for the case-editor byline picker. A flat path rather than a
    # router registration: it lists User rows, not cases, and has no detail /
    # write routes to hang off a viewset.
    path(
        "case-authors/",
        CaseAuthorCandidateView.as_view(),
        name="case-author-candidates",
    ),
    # Public author profile + the cases they wrote.
    path(
        "authors/<slug:slug>/",
        AuthorProfileView.as_view(),
        name="author-profile",
    ),
    path("statistics/", StatisticsView.as_view(), name="statistics"),
    path("feedback/", FeedbackView.as_view(), name="feedback"),
    path("oembed/", OEmbedView.as_view(), name="oembed"),
    path("", include(router.urls)),
]
