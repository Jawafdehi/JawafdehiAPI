from unittest.mock import Mock, patch
from types import SimpleNamespace

import pytest
from django.core.cache import cache

from caseworker.models import LLMProvider, Prompt, PublicChatConfig
from public_chat.query_understanding import QuestionUnderstanding, understand_question


@pytest.fixture
def public_chat_config():
    cache.clear()
    PublicChatConfig.objects.all().delete()
    prompt = Prompt.objects.create(
        name="understanding-test",
        display_name="Understanding Test",
        description="Test prompt",
        prompt="Answer from evidence.",
        model="claude-opus-4-6",
        temperature=0.2,
        max_tokens=1000,
    )
    return PublicChatConfig.objects.create(
        name="understanding-default",
        is_active=True,
        enabled=True,
        prompt=prompt,
    )


def provider(name: str, provider_type: str = "openai", model: str = "gpt-test"):
    return LLMProvider.objects.create(
        name=name,
        display_name=name.replace("-", " ").title(),
        provider_type=provider_type,
        model=model,
        api_key="test-key" if provider_type != "ollama" else "",
        is_active=True,
    )


def fake_llm_service(result):
    service = Mock()
    fallback_provider = SimpleNamespace(
        id=123,
        provider_type="openai",
        model="gpt-test",
        structured_output_mode="auto",
    )
    service.resolve_classifier_provider.side_effect = lambda config: (
        config.classifier_llm_provider or config.llm_provider or fallback_provider
    )
    service.invoke_structured.return_value = result
    return service


@pytest.mark.django_db
def test_understand_question_uses_langchain_structured_plan(public_chat_config):
    understanding = QuestionUnderstanding(
        raw_question="During FY 2079, registrations by category?",
        route="document_rag",
        retrieval_query="2079 registered cases by type annual report",
        normalized_question="2079 cases registered by type",
        language="en",
        years=["2079"],
        needs_count=True,
        needs_type_breakdown=True,
        confidence=0.91,
        reason="annual report data requires knowledge retrieval",
    )
    service = fake_llm_service(understanding)

    with patch("public_chat.query_understanding.LLMService", return_value=service):
        decision = understand_question(
            public_chat_config,
            "During FY 2079, registrations by category?",
        )

    assert decision.route == "document_rag"
    assert decision.retrieval_query == "2079 registered cases by type annual report"
    assert decision.tool_name is None
    assert decision.requires_document_citation is True
    service.invoke_structured.assert_called_once()


@pytest.mark.django_db
def test_understand_question_maps_intents_to_allowlisted_tools(public_chat_config):
    service = fake_llm_service(
        {
            "route": "case_count",
            "retrieval_query": "procurement",
            "tool_name": "private_delete_cases",
            "confidence": 0.7,
            "reason": "count published cases",
        }
    )

    with patch("public_chat.query_understanding.LLMService", return_value=service):
        decision = understand_question(
            public_chat_config, "Procurement complaint volume?"
        )

    assert decision.route == "case_count"
    assert decision.tool_name == "public_count_published_cases"


@pytest.mark.django_db
def test_understand_question_supports_case_get_route(public_chat_config):
    service = fake_llm_service(
        {
            "route": "case_get",
            "case_identifier": "case-0022",
            "confidence": 0.86,
            "reason": "specific published case lookup",
        }
    )

    with patch("public_chat.query_understanding.LLMService", return_value=service):
        decision = understand_question(public_chat_config, "Give me case case-0022")

    assert decision.route == "case_get"
    assert decision.tool_name == "public_get_published_case"
    assert decision.case_identifier == "case-0022"


@pytest.mark.django_db
def test_understand_question_normalizes_case_get_numeric_phrase(public_chat_config):
    service = fake_llm_service(
        {
            "route": "case_get",
            "case_identifier": "case 5",
            "confidence": 0.86,
            "reason": "specific published case lookup",
        }
    )

    with patch("public_chat.query_understanding.LLMService", return_value=service):
        decision = understand_question(public_chat_config, "Tell me about case 5")

    assert decision.route == "case_get"
    assert decision.tool_name == "public_get_published_case"
    assert decision.case_identifier == "5"


@pytest.mark.django_db
def test_understand_question_uses_classifier_provider(public_chat_config):
    answer_provider = provider("answer-provider", "openai", "gpt-answer")
    classifier_provider = provider("classifier-provider", "anthropic", "claude-router")
    public_chat_config.llm_provider = answer_provider
    public_chat_config.classifier_llm_provider = classifier_provider
    public_chat_config.save()
    service = fake_llm_service(
        {
            "route": "case_search",
            "retrieval_query": "procurement complaints",
            "confidence": 0.9,
            "reason": "published case lookup",
        }
    )

    with patch("public_chat.query_understanding.LLMService", return_value=service):
        understand_question(public_chat_config, "Procurement complaints?")

    service.resolve_classifier_provider.assert_any_call(public_chat_config)


@pytest.mark.django_db
def test_understand_question_refuses_when_classifier_fails_and_safety_route_is_uncertain(
    public_chat_config,
):
    service = fake_llm_service(None)
    service.invoke_structured.side_effect = ValueError("structured output failed")

    with patch("public_chat.query_understanding.LLMService", return_value=service):
        decision = understand_question(
            public_chat_config,
            "Tell me about that issue from last time",
        )

    assert decision.route == "clarify"
    assert decision.classifier_source == "refusal"
    assert decision.classifier_error == "ValueError"


@pytest.mark.django_db
def test_understand_question_uses_obvious_deterministic_safety_route(
    public_chat_config,
):
    service = fake_llm_service(None)
    service.invoke_structured.side_effect = ValueError("structured output failed")

    with patch("public_chat.query_understanding.LLMService", return_value=service):
        decision = understand_question(
            public_chat_config,
            "Show person entities related to procurement",
        )

    assert decision.route == "entity_search"
    assert decision.classifier_source == "deterministic_safety"
    assert decision.tool_name == "public_search_jawaf_entities"


@pytest.mark.django_db
def test_understand_question_deterministic_safety_extracts_case_id(
    public_chat_config,
):
    service = fake_llm_service(None)
    service.invoke_structured.side_effect = TimeoutError("classifier timed out")

    with patch("public_chat.query_understanding.LLMService", return_value=service):
        decision = understand_question(public_chat_config, "Tell me about case 5")

    assert decision.route == "case_get"
    assert decision.case_identifier == "5"
    assert decision.classifier_source == "deterministic_safety"


@pytest.mark.django_db
def test_understand_question_deterministic_safety_handles_case_count(
    public_chat_config,
):
    service = fake_llm_service(None)
    service.invoke_structured.side_effect = ValueError("bad schema")

    with patch("public_chat.query_understanding.LLMService", return_value=service):
        decision = understand_question(
            public_chat_config,
            "How many procurement cases are published?",
        )

    assert decision.route == "case_count"
    assert decision.retrieval_query == "procurement"
    assert decision.classifier_source == "deterministic_safety"


@pytest.mark.django_db
def test_understand_question_refuses_low_confidence_uncertain_plans(public_chat_config):
    service = fake_llm_service(
        {
            "route": "case_search",
            "retrieval_query": "that issue",
            "confidence": 0.2,
            "reason": "uncertain",
        }
    )

    with patch("public_chat.query_understanding.LLMService", return_value=service):
        decision = understand_question(public_chat_config, "Tell me about that issue")

    assert decision.route == "clarify"
    assert decision.classifier_source == "refusal"
    assert decision.classifier_error == "LowConfidence"


@pytest.mark.django_db
def test_understand_question_low_confidence_uses_safe_route_when_obvious(
    public_chat_config,
):
    service = fake_llm_service(
        {
            "route": "case_search",
            "retrieval_query": "wrong guess",
            "confidence": 0.2,
            "reason": "uncertain",
        }
    )

    with patch("public_chat.query_understanding.LLMService", return_value=service):
        decision = understand_question(
            public_chat_config,
            "What are the current published Jawafdehi cases?",
        )

    assert decision.route == "case_list"
    assert decision.retrieval_query == ""
    assert decision.classifier_source == "deterministic_safety"


@pytest.mark.django_db
def test_understand_question_classifies_case_list_from_semantic_output(
    public_chat_config,
):
    service = fake_llm_service(
        {
            "route": "case_list",
            "retrieval_query": "",
            "confidence": 0.91,
            "reason": "list published cases",
        }
    )

    with patch("public_chat.query_understanding.LLMService", return_value=service):
        decision = understand_question(
            public_chat_config,
            "What are the current published Jawafdehi cases?",
        )

    assert decision.route == "case_list"
    assert decision.retrieval_query == ""
    assert decision.tool_name == "public_search_published_cases"


@pytest.mark.django_db
def test_understand_question_uses_cache_for_semantic_routes(public_chat_config):
    service = fake_llm_service(
        {
            "route": "case_search",
            "retrieval_query": "procurement complaints",
            "confidence": 0.9,
            "reason": "published case lookup",
        }
    )

    with patch("public_chat.query_understanding.LLMService", return_value=service):
        first = understand_question(public_chat_config, "Procurement complaints?")
        second = understand_question(public_chat_config, "Procurement complaints?")

    assert first.route == "case_search"
    assert second.route == "case_search"
    assert second.classifier_source == "semantic_cache"
    assert service.invoke_structured.call_count == 1


@pytest.mark.django_db
def test_understand_question_cache_is_scoped_to_classifier_provider(public_chat_config):
    first_provider = provider("classifier-a", "openai", "gpt-test-a")
    second_provider = provider("classifier-b", "anthropic", "claude-test-b")
    public_chat_config.classifier_llm_provider = first_provider
    public_chat_config.save()
    service = fake_llm_service(
        {
            "route": "case_search",
            "retrieval_query": "procurement complaints",
            "confidence": 0.9,
            "reason": "published case lookup",
        }
    )

    with patch("public_chat.query_understanding.LLMService", return_value=service):
        first = understand_question(public_chat_config, "Procurement complaints?")
        public_chat_config.classifier_llm_provider = second_provider
        public_chat_config.save()
        second = understand_question(public_chat_config, "Procurement complaints?")

    assert first.route == "case_search"
    assert second.route == "case_search"
    assert service.invoke_structured.call_count == 2


def test_question_understanding_route_mapping_never_uses_external_tool_names():
    understanding = QuestionUnderstanding(
        raw_question="How many procurement cases?",
        normalized_question="procurement cases count",
        route="case_count",
        retrieval_query="procurement",
        reason="count cases",
    )

    decision = understanding.to_route_decision()

    assert decision.tool_name == "public_count_published_cases"
