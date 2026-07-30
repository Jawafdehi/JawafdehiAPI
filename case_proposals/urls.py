from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import CaseUpdateProposalViewSet

router = DefaultRouter()
router.register(
    r"case-update-proposals",
    CaseUpdateProposalViewSet,
    basename="case-update-proposal",
)

urlpatterns = [
    path("", include(router.urls)),
]
