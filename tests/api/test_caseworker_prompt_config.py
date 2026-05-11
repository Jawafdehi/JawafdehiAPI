from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from caseworker.models import Prompt, PublicChatConfig

User = get_user_model()


@pytest.fixture
def staff_client():
    user = User.objects.create_user(
        username="caseworker-admin",
        email="admin@example.com",
        password="testpass123",
        is_staff=True,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_caseworker_admin_can_manage_prompts_skills_and_public_chat_config(
    staff_client,
):
    PublicChatConfig.objects.all().delete()

    skill_response = staff_client.post(
        "/api/caseworker/skills/",
        data={
            "name": "public-citation-style",
            "display_name": "Public Citation Style",
            "description": "Citation style guidance",
            "content": "Cite every factual answer.",
            "is_active": True,
        },
        format="json",
    )
    assert skill_response.status_code == 201

    prompt_response = staff_client.post(
        "/api/caseworker/prompts/",
        data={
            "name": "public-chat-admin",
            "display_name": "Public Chat Admin",
            "description": "Public chat prompt",
            "prompt": "Use public evidence only.",
            "skills": [skill_response.data["id"]],
            "model": "claude-opus-4-6",
            "temperature": 0.2,
            "max_tokens": 1000,
        },
        format="json",
    )
    assert prompt_response.status_code == 201
    assert prompt_response.data["skills"] == [skill_response.data["id"]]

    config_response = staff_client.post(
        "/api/caseworker/public-chat-configs/",
        data={
            "name": "public-chat-default",
            "is_active": True,
            "enabled": True,
            "prompt": prompt_response.data["id"],
            "llm_provider": None,
            "quota_scope": "ip_session",
            "quota_limit": 10,
            "quota_window_seconds": 86400,
            "max_question_chars": 1000,
            "max_history_turns": 6,
            "max_history_chars": 4000,
            "max_tool_calls": 3,
        },
        format="json",
    )
    assert config_response.status_code == 201
    assert config_response.data["prompt"] == prompt_response.data["id"]
    assert config_response.data["quota_limit"] == 10
    assert "classifier_llm_provider" not in config_response.data
    assert "knowledge_rag_enabled" not in config_response.data
    assert "knowledge_collections" not in config_response.data


@pytest.mark.django_db
def test_caseworker_admin_can_manage_multiple_llm_provider_profiles(staff_client):
    first = staff_client.post(
        "/api/caseworker/llm-providers/",
        data={
            "name": "openai-public-answer",
            "display_name": "OpenAI Public Answer",
            "provider_type": "openai",
            "model": "gpt-4o-mini",
            "api_key": "test-key-a",
            "temperature": 0.2,
            "max_tokens": 1000,
            "is_active": True,
            "is_default": True,
            "structured_output_mode": "auto",
        },
        format="json",
    )
    second = staff_client.post(
        "/api/caseworker/llm-providers/",
        data={
            "name": "openai-public-secondary",
            "display_name": "OpenAI Public Secondary",
            "provider_type": "openai",
            "model": "gpt-4o-mini",
            "api_key": "test-key-b",
            "temperature": 0,
            "max_tokens": 500,
            "is_active": True,
            "is_default": False,
            "structured_output_mode": "provider_native",
        },
        format="json",
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.data["provider_type"] == second.data["provider_type"]
    assert first.data["name"] == "openai-public-answer"
    assert "api_key" not in first.data
    assert "api_key" not in second.data

    listed = staff_client.get("/api/caseworker/llm-providers/")
    assert listed.status_code == 200
    assert all("api_key" not in item for item in listed.data["results"])


@pytest.mark.django_db
def test_caseworker_llm_provider_validates_single_default_and_provider_fields(
    staff_client,
):
    first = staff_client.post(
        "/api/caseworker/llm-providers/",
        data={
            "name": "default-provider",
            "provider_type": "openai",
            "model": "gpt-4o-mini",
            "api_key": "test-key-a",
            "is_active": True,
            "is_default": True,
        },
        format="json",
    )
    duplicate_default = staff_client.post(
        "/api/caseworker/llm-providers/",
        data={
            "name": "another-default-provider",
            "provider_type": "anthropic",
            "model": "claude-3-5-sonnet-20241022",
            "api_key": "test-key-b",
            "is_active": True,
            "is_default": True,
        },
        format="json",
    )
    invalid_azure = staff_client.post(
        "/api/caseworker/llm-providers/",
        data={
            "name": "azure-missing-fields",
            "provider_type": "azure",
            "model": "gpt-4o",
            "api_key": "test-key-c",
            "is_active": True,
        },
        format="json",
    )
    invalid_custom = staff_client.post(
        "/api/caseworker/llm-providers/",
        data={
            "name": "custom-missing-base-url",
            "provider_type": "custom",
            "model": "llama",
            "is_active": True,
        },
        format="json",
    )

    assert first.status_code == 201
    assert duplicate_default.status_code == 400
    assert "is_default" in duplicate_default.data
    assert invalid_azure.status_code == 400
    assert {"base_url", "api_version", "deployment_name"} <= set(invalid_azure.data)
    assert invalid_custom.status_code == 400
    assert "base_url" in invalid_custom.data


@pytest.mark.django_db
def test_summary_generation_uses_prompt_id_and_returns_prompt_name(staff_client):
    prompt = Prompt.objects.create(
        name="case-summary-prompt",
        display_name="Case Summary Prompt",
        description="Summary",
        prompt="Summarize {case_data}: {query}",
        model="claude-opus-4-6",
        temperature=0.2,
        max_tokens=1000,
    )

    with (
        patch(
            "caseworker.views.MCPService.retrieve_case_data",
            return_value={"case": "data"},
        ),
        patch(
            "caseworker.views.SummaryGenerationService.generate_summary",
            return_value="Generated summary",
        ),
    ):
        response = staff_client.post(
            "/api/caseworker/summaries/generate/",
            data={
                "case_number": "123-CR-2024",
                "prompt_id": prompt.id,
                "query": "summarize",
            },
            format="json",
        )

    assert response.status_code == 201
    assert response.data["prompt"] == prompt.id
    assert response.data["prompt_name"] == "case-summary-prompt"
    assert response.data["content"] == "Generated summary"


@pytest.mark.django_db
def test_draft_create_uses_prompt_field_and_returns_prompt_name(staff_client):
    prompt = Prompt.objects.create(
        name="draft-prompt",
        display_name="Draft Prompt",
        description="Draft",
        prompt="Draft a response",
        model="claude-opus-4-6",
        temperature=0.2,
        max_tokens=1000,
    )

    response = staff_client.post(
        "/api/caseworker/drafts/",
        data={
            "case_number": "123-CR-2024",
            "prompt": prompt.id,
            "content": "Draft content",
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.data["prompt"] == prompt.id
    assert response.data["prompt_name"] == "draft-prompt"
