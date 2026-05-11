from rest_framework import serializers
from django.contrib.auth.models import User
from .models import (
    MCPServer,
    Prompt,
    Skill,
    Summary,
    Draft,
    DraftVersion,
    LLMProvider,
    PublicChatConfig,
    PROVIDERS_REQUIRING_API_KEY,
    RAGSkillProfile,
)


class CurrentUserSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "email", "role"]

    def get_role(self, obj):
        return "administrator" if obj.is_staff else "caseworker"


class MCPServerSerializer(serializers.ModelSerializer):
    class Meta:
        model = MCPServer
        fields = [
            "id",
            "name",
            "display_name",
            "url",
            "auth_type",
            "auth_token",
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "status", "created_at", "updated_at"]
        extra_kwargs = {"auth_token": {"write_only": True}}


class SkillSerializer(serializers.ModelSerializer):
    class Meta:
        model = Skill
        fields = [
            "id",
            "name",
            "display_name",
            "description",
            "content",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class PromptSerializer(serializers.ModelSerializer):
    class Meta:
        model = Prompt
        fields = [
            "id",
            "name",
            "display_name",
            "description",
            "prompt",
            "skills",
            "model",
            "temperature",
            "max_tokens",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class SummarySerializer(serializers.ModelSerializer):
    prompt_name = serializers.SerializerMethodField()

    class Meta:
        model = Summary
        fields = [
            "id",
            "case_number",
            "prompt",
            "prompt_name",
            "content",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_prompt_name(self, obj):
        """Return prompt name or None if prompt is not set."""
        return obj.prompt.name if obj.prompt else None


class DraftVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = DraftVersion
        fields = ["id", "content", "created_at"]
        read_only_fields = ["id", "created_at"]


class DraftSerializer(serializers.ModelSerializer):
    prompt_name = serializers.SerializerMethodField()
    versions = DraftVersionSerializer(many=True, read_only=True)

    class Meta:
        model = Draft
        fields = [
            "id",
            "case_number",
            "prompt",
            "prompt_name",
            "content",
            "status",
            "external_reference_id",
            "versions",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "external_reference_id",
            "versions",
            "created_at",
            "updated_at",
        ]

    def get_prompt_name(self, obj):
        """Return prompt name or None if prompt is not set."""
        return obj.prompt.name if obj.prompt else None


class LLMProviderSerializer(serializers.ModelSerializer):
    has_api_key = serializers.SerializerMethodField()

    class Meta:
        model = LLMProvider
        fields = [
            "id",
            "name",
            "display_name",
            "provider_type",
            "model",
            "api_key",
            "has_api_key",
            "base_url",
            "api_version",
            "deployment_name",
            "extra_config",
            "temperature",
            "max_tokens",
            "is_active",
            "is_default",
            "structured_output_mode",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "has_api_key", "created_at", "updated_at"]
        extra_kwargs = {"api_key": {"write_only": True}}

    def get_has_api_key(self, obj):
        return obj.has_api_key()

    def validate(self, data):
        """Validate provider-specific admin configuration."""
        provider_type = data.get(
            "provider_type", getattr(self.instance, "provider_type", None)
        )
        api_key = data.get("api_key")
        has_api_key = bool(api_key) or bool(
            self.instance and self.instance.has_api_key()
        )
        base_url = data.get("base_url", getattr(self.instance, "base_url", ""))
        api_version = data.get("api_version", getattr(self.instance, "api_version", ""))
        deployment_name = data.get(
            "deployment_name", getattr(self.instance, "deployment_name", "")
        )
        is_active = data.get("is_active", getattr(self.instance, "is_active", True))
        is_default = data.get("is_default", getattr(self.instance, "is_default", False))
        extra_config = data.get(
            "extra_config", getattr(self.instance, "extra_config", {})
        )

        if provider_type in PROVIDERS_REQUIRING_API_KEY and not has_api_key:
            raise serializers.ValidationError(
                {"api_key": f"API key is required for {provider_type}"}
            )
        if provider_type == "azure":
            errors = {}
            if not base_url:
                errors["base_url"] = "Azure providers require an endpoint/base URL."
            if not api_version:
                errors["api_version"] = "Azure providers require an API version."
            if not deployment_name:
                errors["deployment_name"] = "Azure providers require a deployment name."
            if errors:
                raise serializers.ValidationError(errors)
        if provider_type == "custom" and not base_url:
            raise serializers.ValidationError(
                {"base_url": ("Custom OpenAI-compatible providers require a base URL.")}
            )
        if is_default and not is_active:
            raise serializers.ValidationError(
                {"is_default": "Only an active provider can be the default."}
            )
        if is_default and is_active:
            queryset = LLMProvider.objects.filter(is_active=True, is_default=True)
            if self.instance:
                queryset = queryset.exclude(pk=self.instance.pk)
            if queryset.exists():
                raise serializers.ValidationError(
                    {"is_default": "Only one active provider can be the default."}
                )
        if extra_config is not None and not isinstance(extra_config, dict):
            raise serializers.ValidationError(
                {"extra_config": "Extra config must be a JSON object."}
            )

        return data

    def update(self, instance, validated_data):
        if validated_data.get("api_key") == "":
            validated_data.pop("api_key")
        return super().update(instance, validated_data)


class PublicChatConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = PublicChatConfig
        fields = [
            "id",
            "name",
            "is_active",
            "enabled",
            "prompt",
            "llm_provider",
            "quota_scope",
            "quota_limit",
            "quota_window_seconds",
            "max_question_chars",
            "max_history_turns",
            "max_history_chars",
            "max_tool_calls",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class RAGSkillProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = RAGSkillProfile
        fields = [
            "id",
            "name",
            "display_name",
            "description",
            "skill",
            "collections",
            "trigger_keywords",
            "priority",
            "max_results",
            "min_keyword_matches",
            "requires_citations",
            "is_active",
            "source_path",
            "metadata",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        extra_kwargs = {"collections": {"required": False}}
