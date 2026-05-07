import sys
from types import SimpleNamespace

import pytest
from rest_framework.test import APIClient
from pydantic import BaseModel

from caseworker.models import LLMProvider, Prompt, PublicChatConfig
from caseworker.services import LLMService


class FakeChatModel:
    calls = []
    structured_calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.schema = None
        FakeChatModel.calls.append(kwargs)

    def invoke(self, messages, config=None):
        self.messages = messages
        self.config = config
        if self.schema:
            return {"label": "ok"}
        return SimpleNamespace(content="hello")

    def with_structured_output(self, schema, **kwargs):
        self.schema = schema
        FakeChatModel.structured_calls.append(kwargs)
        return self


class StructuredResult(BaseModel):
    label: str


@pytest.fixture(autouse=True)
def reset_fake_chat_model():
    FakeChatModel.calls = []
    FakeChatModel.structured_calls = []


def provider(name, provider_type="openai", **overrides):
    payload = {
        "name": name,
        "display_name": name,
        "provider_type": provider_type,
        "model": "gpt-test",
        "api_key": "test-key" if provider_type != "ollama" else "",
        "is_active": True,
    }
    payload.update(overrides)
    return LLMProvider.objects.create(**payload)


@pytest.mark.django_db
def test_llm_service_builds_openai_and_custom_chat_models(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "langchain_openai",
        SimpleNamespace(ChatOpenAI=FakeChatModel, AzureChatOpenAI=FakeChatModel),
    )
    openai_provider = provider("openai-answer", "openai")
    custom_provider = provider(
        "custom-local",
        "custom",
        api_key="",
        base_url="http://127.0.0.1:11434/v1",
    )

    service = LLMService()
    service.get_chat_model(openai_provider)
    service.get_chat_model(custom_provider)

    assert FakeChatModel.calls[0]["model"] == "gpt-test"
    assert "base_url" not in FakeChatModel.calls[0]
    assert FakeChatModel.calls[1]["base_url"] == "http://127.0.0.1:11434/v1"


@pytest.mark.django_db
def test_llm_service_invokes_text_and_structured_output(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "langchain_openai",
        SimpleNamespace(ChatOpenAI=FakeChatModel, AzureChatOpenAI=FakeChatModel),
    )
    llm_provider = provider("openai-structured", "openai")

    service = LLMService()
    text = service.invoke_text(
        llm_provider,
        [{"role": "user", "content": "Say hello"}],
        run_name="test-text",
        metadata={"feature": "test"},
    )
    structured = service.invoke_structured(
        llm_provider,
        [{"role": "user", "content": "Classify"}],
        StructuredResult,
        run_name="test-structured",
    )

    assert text == "hello"
    assert structured == StructuredResult(label="ok")


@pytest.mark.django_db
def test_llm_service_honors_structured_output_mode(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "langchain_openai",
        SimpleNamespace(ChatOpenAI=FakeChatModel, AzureChatOpenAI=FakeChatModel),
    )
    llm_provider = provider(
        "openai-provider-native",
        "openai",
        structured_output_mode="provider_native",
    )

    structured = LLMService().invoke_structured(
        llm_provider,
        [{"role": "user", "content": "Classify"}],
        StructuredResult,
        run_name="test-structured-provider-native",
    )

    assert structured == StructuredResult(label="ok")
    assert FakeChatModel.structured_calls == [{"method": "json_schema"}]


@pytest.mark.django_db
def test_llm_service_resolves_public_chat_provider_fallbacks():
    PublicChatConfig.objects.all().delete()
    default_provider = provider("default-answer", "openai", is_default=True)
    answer_provider = provider("configured-answer", "openai")
    classifier_provider = provider("configured-classifier", "anthropic")
    prompt = Prompt.objects.create(
        name="provider-fallback-prompt",
        display_name="Provider Fallback Prompt",
        description="Prompt",
        prompt="Answer from evidence.",
        model="unused",
    )
    config = PublicChatConfig.objects.create(
        name="provider-fallback-config",
        prompt=prompt,
        llm_provider=answer_provider,
        classifier_llm_provider=classifier_provider,
    )

    service = LLMService()

    assert service.resolve_answer_provider(config) == answer_provider
    assert service.resolve_classifier_provider(config) == classifier_provider

    config.classifier_llm_provider = None
    config.llm_provider = None
    assert service.resolve_answer_provider(config) == default_provider
    assert service.resolve_classifier_provider(config) == default_provider

    answer_provider.is_active = False
    answer_provider.save()
    classifier_provider.is_active = False
    classifier_provider.save()
    config.classifier_llm_provider = classifier_provider
    config.llm_provider = answer_provider
    assert service.resolve_answer_provider(config) == default_provider
    assert service.resolve_classifier_provider(config) == default_provider


@pytest.mark.django_db
def test_llm_provider_list_tolerates_unreadable_encrypted_api_key(django_user_model):
    llm_provider = provider("unreadable-key", "google")
    LLMProvider.objects.filter(pk=llm_provider.pk).update(
        api_key="enc:v1:not-a-valid-fernet-token"
    )

    admin_user = django_user_model.objects.create_user(
        username="llm-admin",
        password="test-password",
        is_staff=True,
    )
    client = APIClient()
    client.force_authenticate(admin_user)
    response = client.get("/api/caseworker/llm-providers/")

    assert response.status_code == 200
    payload = (
        response.data["results"][0] if "results" in response.data else response.data[0]
    )
    assert payload["name"] == "unreadable-key"
    assert payload["has_api_key"] is False


@pytest.mark.django_db
def test_llm_service_unsupported_provider_raises_clear_error():
    llm_provider = provider("unsupported-provider", "openai")
    llm_provider.provider_type = "unsupported"

    with pytest.raises(ValueError, match="Unsupported provider type"):
        LLMService().get_chat_model(llm_provider)
