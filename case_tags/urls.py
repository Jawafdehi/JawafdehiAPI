from django.urls import include, path
from rest_framework.routers import DefaultRouter

from case_tags.views import TagProposalViewSet, VocabularyView

router = DefaultRouter()
router.register(r"case-tag-proposals", TagProposalViewSet, basename="case-tag-proposal")

urlpatterns = [
    path("case-tags/", VocabularyView.as_view(), name="case-tags"),
    path("", include(router.urls)),
]
