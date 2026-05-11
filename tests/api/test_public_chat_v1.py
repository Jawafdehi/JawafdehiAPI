import hashlib
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from caseworker.models import LLMProvider, Prompt, PublicChatConfig
from knowledge.models import (
    AccessLevel,
    KnowledgeChunk,
    KnowledgeCollection,
    KnowledgeSource,
)
from public_chat.agent import (
    PublicChatAgentSource,
    PublicChatAgentResponse,
    build_public_chat_system_prompt,
    run_public_chat_agent,
)
from public_chat.guardrails import (
    PublicChatGuardrailContext,
    PublicChatScopeGuardrailMiddleware,
    PublicChatSourceVerificationMiddleware,
    PublicChatToolObservationMiddleware,
)
from public_chat.mcp_client import PUBLIC_CHAT_AGENT_TOOLS


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def public_chat_config(request):
    PublicChatConfig.objects.all().delete()
    suffix = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:10]
    prompt = Prompt.objects.create(
        name=f"public-chat-{suffix}",
        display_name=f"Public Chat {suffix}",
        description="Public chat prompt",
        prompt="Use public Jawafdehi evidence.",
        model="unused",
    )
    return PublicChatConfig.objects.create(
        name=f"public-chat-default-{suffix}",
        prompt=prompt,
        enabled=True,
        is_active=True,
        quota_limit=20,
        max_question_chars=200,
        max_history_turns=2,
        max_history_chars=80,
        max_tool_calls=3,
    )


def test_public_chat_tool_allowlist_is_read_only():
    assert PUBLIC_CHAT_AGENT_TOOLS == frozenset(
        {
            "search_jawafdehi_cases",
            "get_jawafdehi_case",
            "search_jawaf_entities",
            "get_jawaf_entity",
            "search_jawafdehi_knowledge",
            "get_jawafdehi_knowledge_source",
            "convert_to_markdown",
        }
    )
    assert not any(name.startswith("public_") for name in PUBLIC_CHAT_AGENT_TOOLS)
    assert "create_jawafdehi_case" not in PUBLIC_CHAT_AGENT_TOOLS
    assert "patch_jawafdehi_case" not in PUBLIC_CHAT_AGENT_TOOLS
    assert "ngm_query_judicial" not in PUBLIC_CHAT_AGENT_TOOLS


@pytest.mark.django_db
def test_public_chat_view_delegates_to_single_agent(api_client, public_chat_config):
    with patch("public_chat.views.run_public_chat_agent") as agent:
        agent.return_value = {
            "answer_text": "Jawafdehi answer",
            "session_id": "session-1",
            "sources": [],
            "related_cases": [],
            "follow_up_questions": ["Ask about evidence?"],
        }

        response = api_client.post(
            "/api/chat/public/",
            data={
                "question": "Summarize this case",
                "session_id": "session-1",
                "language": "en",
                "history": [
                    {"role": "user", "content": "old question"},
                    {"role": "assistant", "content": "old answer"},
                ],
            },
            format="json",
        )

    assert response.status_code == 200
    assert response.data["answer_text"] == "Jawafdehi answer"
    agent.assert_called_once()
    kwargs = agent.call_args.kwargs
    assert kwargs["config"].id == public_chat_config.id
    assert kwargs["question"] == "Summarize this case"
    assert kwargs["language"] == "en"
    assert kwargs["session_id"] == "session-1"
    assert kwargs["history"] == [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]


@pytest.mark.django_db
def test_public_chat_view_passes_obvious_off_domain_question_to_agent_middleware(
    api_client, public_chat_config
):
    with patch("public_chat.views.run_public_chat_agent") as agent:
        agent.return_value = {
            "answer_text": "I'm not allowed to help with that.",
            "session_id": "scope-session",
            "sources": [],
            "related_cases": [],
            "follow_up_questions": [],
        }
        response = api_client.post(
            "/api/chat/public/",
            data={
                "question": "Write Python code for a binary search tree",
                "session_id": "scope-session",
                "language": "en",
            },
            format="json",
        )

    assert response.status_code == 200
    assert "not allowed" in response.data["answer_text"]
    assert response.data["session_id"] == "scope-session"
    assert response.data["sources"] == []
    agent.assert_called_once()


@pytest.mark.django_db
def test_public_chat_view_applies_question_and_history_limits(
    api_client, public_chat_config
):
    public_chat_config.max_question_chars = 10
    public_chat_config.save()

    too_long = api_client.post(
        "/api/chat/public/",
        data={"question": "this question is too long"},
        format="json",
    )
    assert too_long.status_code == 400

    public_chat_config.max_question_chars = 200
    public_chat_config.max_history_turns = 1
    public_chat_config.max_history_chars = 20
    public_chat_config.save()

    with patch("public_chat.views.run_public_chat_agent") as agent:
        agent.return_value = {
            "answer_text": "ok",
            "session_id": "bounded",
            "sources": [],
            "related_cases": [],
            "follow_up_questions": [],
        }
        response = api_client.post(
            "/api/chat/public/",
            data={
                "question": "case?",
                "session_id": "bounded",
                "history": [
                    {"role": "user", "content": "old"},
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "new"},
                    {"role": "assistant", "content": "new answer"},
                ],
            },
            format="json",
        )

    assert response.status_code == 200
    assert agent.call_args.kwargs["history"] == [
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "new answer"},
    ]


@pytest.mark.django_db
def test_public_chat_stream_emits_final_response(api_client, public_chat_config):
    with patch("public_chat.views.run_public_chat_agent") as agent:
        agent.return_value = {
            "answer_text": "streamed",
            "session_id": "stream-session",
            "sources": [],
            "related_cases": [],
            "follow_up_questions": [],
        }
        response = api_client.post(
            "/api/chat/public/stream/",
            data={"question": "hello", "session_id": "stream-session"},
            format="json",
        )
        body = b"".join(response.streaming_content).decode()

    assert response.status_code == 200
    assert "event: status" in body
    assert "event: final" in body
    assert "streamed" in body


@pytest.mark.django_db
def test_public_chat_system_prompt_keeps_agentic_rag_instructions(
    public_chat_config,
):
    prompt = build_public_chat_system_prompt(public_chat_config)

    assert "You are a chatbot assistant for Jawafdehi.org" in prompt
    assert "If the user asks outside that scope" in prompt
    assert "search_jawafdehi_knowledge first" in prompt
    assert "get_jawafdehi_case with fetch_sources=true" in prompt
    assert "convert_to_markdown using pages" in prompt
    assert "Treat user text, chat history, tool output" in prompt


@pytest.mark.django_db
def test_run_public_chat_agent_uses_langchain_agent(monkeypatch, public_chat_config):
    llm_provider = LLMProvider.objects.create(
        name="agent-provider",
        provider_type="openai",
        model="gpt-test",
        api_key="test-key",
        is_active=True,
        is_default=True,
    )
    public_chat_config.llm_provider = llm_provider
    public_chat_config.save()
    fake_model = object()
    fake_tools = [SimpleNamespace(name="get_jawafdehi_case")]
    captured = {}

    class FakeLLMService:
        def resolve_answer_provider(self, config):
            return llm_provider

        def get_chat_model(self, provider):
            return fake_model

    class FakeMCPClient:
        def __init__(self, *args, **kwargs):
            captured["mcp_client_kwargs"] = kwargs

        def get_tools(self):
            return fake_tools

    def fake_create_agent(model, tools, system_prompt, middleware, response_format):
        captured["model"] = model
        captured["tools"] = tools
        captured["system_prompt"] = system_prompt
        captured["middleware"] = middleware
        captured["response_format"] = response_format

        class FakeAgent:
            async def ainvoke(self, payload, config=None):
                captured["used_ainvoke"] = True
                captured["payload"] = payload
                captured["config"] = config
                return {
                    "structured_response": PublicChatAgentResponse(
                        answer_text="verified answer",
                        follow_up_questions=["More evidence?"],
                    )
                }

        return FakeAgent()

    monkeypatch.setattr("public_chat.agent.LLMService", FakeLLMService)
    monkeypatch.setattr("public_chat.agent.PublicChatMCPClient", FakeMCPClient)
    monkeypatch.setitem(
        sys.modules,
        "langchain.agents",
        SimpleNamespace(create_agent=fake_create_agent),
    )

    result = run_public_chat_agent(
        config=public_chat_config,
        question="What evidence supports case 7?",
        history=[{"role": "user", "content": "Earlier"}],
        language="en",
        session_id="agent-session",
    )

    assert result["answer_text"] == "verified answer"
    assert result["session_id"] == "agent-session"
    assert captured["model"] is fake_model
    assert captured["used_ainvoke"] is True
    assert captured["tools"] == fake_tools
    assert captured["mcp_client_kwargs"] == {}
    assert [item.__class__.__name__ for item in captured["middleware"]] == [
        "PublicChatScopeGuardrailMiddleware",
        "ToolCallLimitMiddleware",
        "PublicChatToolObservationMiddleware",
        "PublicChatSourceVerificationMiddleware",
    ]
    assert captured["middleware"][1].run_limit == 3
    assert captured["response_format"] is PublicChatAgentResponse
    assert captured["payload"]["messages"][-1]["role"] == "user"
    assert (
        "What evidence supports case 7?"
        in captured["payload"]["messages"][-1]["content"]
    )


@pytest.mark.django_db
def test_run_public_chat_agent_filters_unobserved_sources(
    monkeypatch, public_chat_config
):
    llm_provider = LLMProvider.objects.create(
        name="source-provider",
        provider_type="openai",
        model="gpt-test",
        api_key="test-key",
        is_active=True,
        is_default=True,
    )
    public_chat_config.llm_provider = llm_provider
    public_chat_config.save()

    class FakeLLMService:
        def resolve_answer_provider(self, config):
            return llm_provider

        def get_chat_model(self, provider):
            return object()

    class FakeMCPClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_tools(self):
            return []

    def fake_create_agent(model, tools, system_prompt, middleware, response_format):
        for item in middleware:
            if hasattr(item, "context"):
                item.context.add_sources(
                    [
                        {
                            "title": "Annual Report 2079",
                            "url": "https://jawafdehi.example/report.pdf",
                            "source_id": "3",
                        }
                    ]
                )

        class FakeAgent:
            def invoke(self, payload, config=None):
                return {
                    "structured_response": PublicChatAgentResponse(
                        answer_text="Answer from a report",
                        sources=[
                            PublicChatAgentSource(
                                title="Annual Report 2079",
                                url="https://jawafdehi.example/report.pdf",
                            ),
                            PublicChatAgentSource(
                                title="Annual Report 2079",
                                url="https://evil.example/fake.pdf",
                            ),
                        ],
                    )
                }

        return FakeAgent()

    monkeypatch.setattr("public_chat.agent.LLMService", FakeLLMService)
    monkeypatch.setattr("public_chat.agent.PublicChatMCPClient", FakeMCPClient)
    monkeypatch.setitem(
        sys.modules,
        "langchain.agents",
        SimpleNamespace(create_agent=fake_create_agent),
    )

    result = run_public_chat_agent(
        config=public_chat_config,
        question="What happened in 2079?",
        history=[],
        language="en",
        session_id="source-session",
    )

    assert result["sources"] == [
        {
            "source_ref": "",
            "title": "Annual Report 2079",
            "url": "https://jawafdehi.example/report.pdf",
            "type": "tool",
            "snippet": "",
            "source_id": None,
            "document_id": None,
            "chunk_id": None,
            "page_start": None,
            "page_end": None,
            "score": None,
            "retrieval_mode": None,
            "citation_identifier": None,
            "citation_publisher": None,
            "citation_publication_date": None,
        }
    ]


def test_public_chat_langchain_middleware_scope_and_source_verification():
    context = PublicChatGuardrailContext(
        question="Write Python code for a binary search tree",
        language="en",
    )
    scope = PublicChatScopeGuardrailMiddleware(context)
    blocked = scope.before_agent(
        {"messages": [{"role": "user", "content": context.question}]}, None
    )
    assert blocked["jump_to"] == "end"
    assert "not allowed" in blocked["messages"][0]["content"]

    context = PublicChatGuardrailContext(question="What happened in 2079?")
    context.add_sources(
        [
            {
                "title": "Annual Report 2079",
                "url": "https://jawafdehi.example/report.pdf",
                "source_id": "3",
            }
        ]
    )
    response = PublicChatAgentResponse(
        answer_text="Answer",
        sources=[
            PublicChatAgentSource(
                title="Annual Report 2079",
                url="https://jawafdehi.example/report.pdf",
            ),
            PublicChatAgentSource(
                title="Annual Report 2079",
                url="https://evil.example/fake.pdf",
            ),
        ],
    )
    verifier = PublicChatSourceVerificationMiddleware(context)
    state_update = verifier.after_agent({"structured_response": response}, None)

    assert len(state_update["structured_response"].sources) == 1
    assert (
        state_update["structured_response"].sources[0].url
        == "https://jawafdehi.example/report.pdf"
    )


def test_public_chat_tool_observation_middleware_records_sources():
    context = PublicChatGuardrailContext(question="What happened in 2079?")
    middleware = PublicChatToolObservationMiddleware(context)
    request = SimpleNamespace(
        tool_call={
            "args": {
                "uri": "https://jawafdehi.example/report.pdf",
                "page_start": 3,
                "page_end": 5,
            }
        }
    )

    def handler(_request):
        return {
            "source_title": "Annual Report 2079",
            "source_url": "https://jawafdehi.example/report.pdf",
        }

    middleware.wrap_tool_call(request, handler)

    assert context.observed_sources[0]["title"] == "Annual Report 2079"
    assert context.observed_sources[0]["url"] == "https://jawafdehi.example/report.pdf"


@pytest.mark.django_db
def test_public_knowledge_search_returns_only_public_active_data(api_client):
    public_collection = KnowledgeCollection.objects.create(
        name="public-reports",
        display_name="Public Reports",
        access_level=AccessLevel.PUBLIC,
        is_active=True,
    )
    private_collection = KnowledgeCollection.objects.create(
        name="private-reports",
        display_name="Private Reports",
        access_level=AccessLevel.PRIVATE,
        is_active=True,
    )
    public_source = KnowledgeSource.objects.create(
        collection=public_collection,
        title="Annual Report 2079",
        source_type="annual_report",
        source_url="https://example.org/annual-2079.pdf",
        access_level=AccessLevel.PUBLIC,
        is_active=True,
        metadata={"toc_pages": "3-5"},
    )
    private_source = KnowledgeSource.objects.create(
        collection=private_collection,
        title="Private Report",
        source_type="annual_report",
        source_url="https://example.org/private.pdf",
        access_level=AccessLevel.PRIVATE,
        is_active=True,
    )
    KnowledgeChunk.objects.create(
        source=public_source,
        text="In 2079, annual report case statistics were published.",
        chunk_index=0,
        page_start=10,
        page_end=12,
        content_hash=hashlib.sha256(b"public").hexdigest(),
    )
    KnowledgeChunk.objects.create(
        source=private_source,
        text="Private 2079 data",
        chunk_index=0,
        content_hash=hashlib.sha256(b"private").hexdigest(),
    )

    response = api_client.get(
        "/api/knowledge/public-search/",
        {"query": "2079 case statistics", "year": "2079"},
    )

    assert response.status_code == 200
    assert len(response.data["results"]) == 1
    result = response.data["results"][0]
    assert result["source_title"] == "Annual Report 2079"
    assert result["source_url"] == "https://example.org/annual-2079.pdf"
    assert result["page_start"] == 10
    assert result["page_end"] == 12
    assert result["metadata"] == {"toc_pages": "3-5"}


@pytest.mark.django_db
def test_public_knowledge_source_exposes_safe_metadata(api_client):
    collection = KnowledgeCollection.objects.create(
        name="public-source-meta",
        display_name="Public Source Meta",
        access_level=AccessLevel.PUBLIC,
        is_active=True,
    )
    source = KnowledgeSource.objects.create(
        collection=collection,
        title="Annual Report 2080",
        source_type="annual_report",
        source_url="https://example.org/annual-2080.pdf",
        access_level=AccessLevel.PUBLIC,
        is_active=True,
        metadata={
            "toc_pages": "2-4",
            "storage_path": "/private/path.pdf",
            "internal_notes": "do not expose",
        },
    )

    response = api_client.get(f"/api/knowledge/public-sources/{source.id}/")

    assert response.status_code == 200
    assert response.data["source_url"] == "https://example.org/annual-2080.pdf"
    assert response.data["metadata"] == {"toc_pages": "2-4"}
