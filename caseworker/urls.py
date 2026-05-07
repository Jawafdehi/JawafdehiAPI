from django.urls import path, include
from rest_framework.routers import SimpleRouter
from .views import (
    UserViewSet,
    QueryViewSet,
    MCPServerViewSet,
    PromptViewSet,
    SkillViewSet,
    SummaryViewSet,
    DraftViewSet,
    LLMProviderViewSet,
    PublicChatConfigViewSet,
    RAGSkillProfileViewSet,
)

router = SimpleRouter()
router.register(r"users", UserViewSet, basename="cw-user")
router.register(r"queries", QueryViewSet, basename="cw-query")
router.register(r"mcp-servers", MCPServerViewSet, basename="cw-mcp-server")
router.register(r"prompts", PromptViewSet, basename="cw-prompt")
router.register(r"skills", SkillViewSet, basename="cw-skill")
router.register(r"summaries", SummaryViewSet, basename="cw-summary")
router.register(r"drafts", DraftViewSet, basename="cw-draft")
router.register(r"llm-providers", LLMProviderViewSet, basename="cw-llm-provider")
router.register(
    r"public-chat-configs",
    PublicChatConfigViewSet,
    basename="cw-public-chat-config",
)
router.register(
    r"rag-skill-profiles",
    RAGSkillProfileViewSet,
    basename="cw-rag-skill-profile",
)

urlpatterns = [
    path("", include(router.urls)),
]
