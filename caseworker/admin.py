from django.contrib import admin
from .models import (
    MCPServer,
    Prompt,
    Skill,
    Summary,
    Draft,
    DraftVersion,
    LLMProvider,
    PublicChatConfig,
    RAGSkillProfile,
)


@admin.register(MCPServer)
class MCPServerAdmin(admin.ModelAdmin):
    list_display = ["name", "display_name", "url", "auth_type", "status", "created_at"]
    list_filter = ["status", "auth_type"]
    search_fields = ["name"]
    # Exclude auth_token from admin forms to prevent displaying secrets
    exclude = ["auth_token"]
    readonly_fields = ["status", "created_at", "updated_at"]


@admin.register(Prompt)
class PromptAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "display_name",
        "model",
        "temperature",
        "max_tokens",
        "created_at",
    ]
    search_fields = ["name", "description"]
    filter_horizontal = ["skills"]


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ["name", "display_name", "is_active", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["name", "display_name", "description", "content"]


@admin.register(Summary)
class SummaryAdmin(admin.ModelAdmin):
    list_display = ["case_number", "user", "prompt", "created_at"]
    list_filter = ["prompt"]
    search_fields = ["case_number", "user__username"]


@admin.register(Draft)
class DraftAdmin(admin.ModelAdmin):
    list_display = ["case_number", "user", "prompt", "status", "created_at"]
    list_filter = ["status", "prompt"]
    search_fields = ["case_number", "user__username"]


@admin.register(DraftVersion)
class DraftVersionAdmin(admin.ModelAdmin):
    list_display = ["draft", "created_at"]
    list_filter = []


@admin.register(LLMProvider)
class LLMProviderAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "display_name",
        "provider_type",
        "model",
        "temperature",
        "max_tokens",
        "is_active",
        "is_default",
        "structured_output_mode",
        "created_at",
    ]
    list_filter = [
        "provider_type",
        "is_active",
        "is_default",
        "structured_output_mode",
    ]
    search_fields = ["name", "display_name", "model", "base_url"]
    # Exclude api_key from admin forms to prevent displaying secrets
    exclude = ["api_key"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(PublicChatConfig)
class PublicChatConfigAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "enabled",
        "is_active",
        "prompt",
        "llm_provider",
        "classifier_llm_provider",
        "quota_scope",
        "quota_limit",
        "quota_window_seconds",
        "knowledge_rag_enabled",
        "max_knowledge_results",
    ]
    list_filter = ["enabled", "is_active", "quota_scope", "knowledge_rag_enabled"]
    search_fields = ["name", "prompt__name", "prompt__display_name"]
    filter_horizontal = ["knowledge_collections", "rag_skill_profiles"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(RAGSkillProfile)
class RAGSkillProfileAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "display_name",
        "skill",
        "priority",
        "max_results",
        "min_keyword_matches",
        "requires_citations",
        "is_active",
    ]
    list_filter = ["is_active", "requires_citations"]
    search_fields = ["name", "display_name", "description", "trigger_keywords"]
    filter_horizontal = ["collections"]
    readonly_fields = ["created_at", "updated_at"]
