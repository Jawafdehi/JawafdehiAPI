"""
URL configuration for the cases app API.

See: .kiro/specs/accountability-platform-core/design.md
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .api_views import (
    CaseViewSet,
    FeedbackView,
    NewsletterSubscriptionView,
    NewsletterUnsubscribeView,
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

urlpatterns = [
    # NOTE: ``search/`` is no longer mounted here. The platform-wide unified
    # search moved to the ``search`` app (``GET /api/search/``, see
    # config/urls.py + search), replacing this cases-scoped
    # in-process search in the OpenSearch cutover (plan decision #5).
    path("statistics/", StatisticsView.as_view(), name="statistics"),
    path("feedback/", FeedbackView.as_view(), name="feedback"),
    path(
        "newsletter/subscriptions/",
        NewsletterSubscriptionView.as_view(),
        name="newsletter-subscriptions",
    ),
    path(
        "newsletter/unsubscribe/<uuid:token>/",
        NewsletterUnsubscribeView.as_view(),
        name="newsletter-unsubscribe",
    ),
    path("oembed/", OEmbedView.as_view(), name="oembed"),
    path("", include(router.urls)),
]
