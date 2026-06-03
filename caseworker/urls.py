from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .views import (
    DraftViewSet,
    LLMProviderViewSet,
    MCPServerViewSet,
    MeView,
    QueryViewSet,
    SkillViewSet,
    SummaryViewSet,
    UserViewSet,
)

router = SimpleRouter()
router.register(r"users", UserViewSet, basename="cw-user")
router.register(r"queries", QueryViewSet, basename="cw-query")
router.register(r"mcp-servers", MCPServerViewSet, basename="cw-mcp-server")
router.register(r"skills", SkillViewSet, basename="cw-skill")
router.register(r"summaries", SummaryViewSet, basename="cw-summary")
router.register(r"drafts", DraftViewSet, basename="cw-draft")
router.register(r"llm-providers", LLMProviderViewSet, basename="cw-llm-provider")

urlpatterns = [
    path("me", MeView.as_view(), name="cw-me"),
    path("", include(router.urls)),
]
