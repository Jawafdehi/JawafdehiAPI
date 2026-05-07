import logging
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.core.cache import caches
from django.test import override_settings
from rest_framework.test import APIClient

from caseworker.models import Prompt, PublicChatConfig, RAGSkillProfile, Skill
from config.mcp_servers import build_public_chat_mcp_servers
from knowledge.models import (
    AccessLevel,
    KnowledgeChunk,
    KnowledgeCollection,
    KnowledgeSource,
)
from public_chat.mcp_client import PublicChatMCPClient, PublicChatMCPError
from public_chat import mcp_client as public_chat_mcp_client
from public_chat.quota import QUOTA_CACHE_NAME, check_and_increment_quota
from public_chat.routing import PUBLIC_CHAT_MCP_TOOLS, QueryPlan, route_question

PUBLIC_CHAT_URL = "/api/chat/public/"


@pytest.fixture
def public_chat_config():
    caches["default"].clear()
    caches[QUOTA_CACHE_NAME].clear()
    PublicChatConfig.objects.all().delete()
    prompt = Prompt.objects.create(
        name="public-chat-test",
        display_name="Public Chat Test",
        description="Test prompt",
        prompt="Configured public prompt. Answer only from supplied evidence.",
        model="claude-opus-4-6",
        temperature=0.2,
        max_tokens=1000,
    )
    return PublicChatConfig.objects.create(
        name="default-test",
        is_active=True,
        enabled=True,
        prompt=prompt,
        quota_scope="ip_session",
        quota_limit=10,
        quota_window_seconds=86400,
        max_question_chars=1000,
        max_history_turns=6,
        max_history_chars=4000,
        max_mcp_results=5,
        max_tool_calls=3,
        max_evidence_chars=8000,
    )


def published_case_payload(**overrides):
    payload = {
        "id": 42,
        "case_id": "case-42",
        "slug": "procurement-case",
        "state": "PUBLISHED",
        "title": "Published procurement case",
        "short_description": "Public summary",
        "description": "Public description",
    }
    payload.update(overrides)
    return payload


def test_public_chat_mcp_config_defaults_to_workflow_stdio_shape():
    servers = build_public_chat_mcp_servers(
        api_base_url="http://127.0.0.1:8000",
        allow_default_stdio=True,
    )

    assert servers == {
        "jawafdehi": {
            "command": "uvx",
            "args": [
                "--from",
                "git+https://github.com/Jawafdehi/jawafdehi-mcp.git",
                "jawafdehi-mcp",
            ],
            "transport": "stdio",
            "env": {"JAWAFDEHI_API_BASE_URL": "http://127.0.0.1:8000"},
        }
    }
    assert "JAWAFDEHI_API_TOKEN" not in servers["jawafdehi"]["env"]


def test_public_chat_mcp_config_does_not_create_stdio_default_for_production():
    servers = build_public_chat_mcp_servers(
        api_base_url="https://api.example",
        allow_default_stdio=False,
    )

    assert servers == {}


def test_public_chat_mcp_config_supports_full_env_override():
    servers = build_public_chat_mcp_servers(
        api_base_url="http://127.0.0.1:8000",
        servers_json=(
            '{"jawafdehi": {"command": "uv", "args": ["run", "--directory", '
            '"/tmp/jawafdehi-mcp", "jawafdehi-mcp"], "transport": "stdio", '
            '"env": {"JAWAFDEHI_API_BASE_URL": "http://127.0.0.1:8000"}}}'
        ),
    )

    assert servers["jawafdehi"]["command"] == "uv"
    assert servers["jawafdehi"]["args"] == [
        "run",
        "--directory",
        "/tmp/jawafdehi-mcp",
        "jawafdehi-mcp",
    ]
    assert servers["jawafdehi"]["transport"] == "stdio"
    assert servers["jawafdehi"]["env"] == {
        "JAWAFDEHI_API_BASE_URL": "http://127.0.0.1:8000"
    }


def test_public_chat_routing_owns_public_mcp_tool_policy():
    assert PUBLIC_CHAT_MCP_TOOLS == frozenset(
        {
            "public_count_published_cases",
            "public_search_published_cases",
            "public_get_published_case",
            "public_search_jawaf_entities",
        }
    )
    assert (
        route_question("How many procurement cases are published?").tool_name
        == "public_count_published_cases"
    )
    assert (
        route_question("Show person entities related to procurement").tool_name
        == "public_search_jawaf_entities"
    )
    assert route_question("Show case case-42 details").tool_name == (
        "public_get_published_case"
    )
    slug_route = route_question(
        "Tell me about /case/sandeep-lamichhane-case-media-trial-and-public-opinion"
    )
    assert slug_route.route == "case_get"
    assert (
        slug_route.case_identifier
        == "sandeep-lamichhane-case-media-trial-and-public-opinion"
    )
    assert (
        route_question("Tell me about sandeep-lamichhane-case-media-trial").route
        == "clarify"
    )
    list_route = route_question("What are the current published Jawafdehi cases?")
    assert list_route.route == "case_list"
    assert list_route.retrieval_query == ""
    assert list_route.tool_name == "public_search_published_cases"
    report_route = route_question("What is in the 2078 annual report?")
    assert report_route.route == "document_rag"
    assert report_route.tool_name is None
    assert route_question("procurement cases").route == "clarify"
    assert (
        route_question("procurement cases", default_to_case_search=True).route
        == "case_search"
    )


def test_mcp_client_parses_json_text_content():
    parsed = PublicChatMCPClient()._parse_tool_result(
        [{"type": "text", "text": '{"count": 1, "results": []}'}]
    )

    assert parsed == {"count": 1, "results": []}


def test_mcp_client_plain_text_errors_become_public_errors():
    with pytest.raises(PublicChatMCPError, match="Error accessing public cases API"):
        PublicChatMCPClient()._parse_tool_result(
            [{"type": "text", "text": "Error accessing public cases API: timeout"}]
        )


@override_settings(
    DEBUG=False,
    PUBLIC_CHAT_MCP_SERVERS={
        "jawafdehi": {"transport": "stdio", "command": "uvx", "args": []}
    },
)
def test_mcp_client_rejects_stdio_transport_in_production():
    with pytest.raises(PublicChatMCPError, match="managed non-stdio"):
        PublicChatMCPClient(allowed_tools={"public_search_published_cases"}).call_tool(
            "public_search_published_cases",
            {"page": 1},
        )


@override_settings(PUBLIC_CHAT_MCP_TOOL_CACHE_SECONDS=60)
def test_mcp_client_caches_non_stdio_tool_maps():
    class FakeTool:
        name = "public_search_published_cases"
        handle_tool_error = False

        async def ainvoke(self, arguments):
            return {"results": []}

    class FakeClient:
        calls = 0

        def __init__(self, servers):
            self.servers = servers

        async def get_tools(self):
            type(self).calls += 1
            return [FakeTool()]

    public_chat_mcp_client._TOOL_CACHE.clear()
    servers = {
        "jawafdehi": {
            "transport": "streamable_http",
            "url": "https://mcp.example/mcp",
        }
    }
    client = PublicChatMCPClient(allowed_tools={"public_search_published_cases"})
    load_tool_map = async_to_sync(client._load_tool_map)

    first = load_tool_map(FakeClient, servers)
    second = load_tool_map(FakeClient, servers)

    assert FakeClient.calls == 1
    assert (
        first["public_search_published_cases"]
        is second["public_search_published_cases"]
    )


@pytest.mark.django_db
def test_public_chat_uses_configured_prompt_and_active_skills(public_chat_config):
    active_skill = Skill.objects.create(
        name="public-citations",
        display_name="Public Citations",
        description="Citation instruction",
        content="Cite the retrieved public source.",
        is_active=True,
    )
    inactive_skill = Skill.objects.create(
        name="inactive-skill",
        display_name="Inactive Skill",
        description="Inactive",
        content="This must not be loaded.",
        is_active=False,
    )
    public_chat_config.prompt.skills.add(active_skill, inactive_skill)
    captured = {}

    def fake_generate_answer(config, prompt):
        captured["prompt"] = prompt
        return "There is one supported published procurement case."

    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question("How many procurement cases are published?"),
        ),
        patch(
            "public_chat.views.PublicChatMCPClient.call_tool",
            return_value={
                "published_count": 1,
                "count_scope": "published_only",
                "results": [published_case_payload()],
            },
        ) as mcp_call,
        patch("public_chat.views.generate_answer", side_effect=fake_generate_answer),
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={
                "question": "How many procurement cases are published?",
                "session_id": "session-a",
                "history": [],
                "language": "en",
            },
            format="json",
        )

    assert response.status_code == 200
    assert "Configured public prompt" in captured["prompt"]
    assert "Cite the retrieved public source" in captured["prompt"]
    assert '"route": "case_count"' in captured["prompt"]
    assert '"count": 1' in captured["prompt"]
    assert "use the evidence.count value exactly" in captured["prompt"]
    assert "This must not be loaded" not in captured["prompt"]
    assert response.data["sources"][0]["type"] == "case"
    assert response.data["sources"][0]["url"] == "/case/42"
    assert response.data["related_cases"][0]["case_id"] == "case-42"
    assert response.data["related_cases"][0]["url"] == "/case/42"
    mcp_call.assert_called_once_with(
        "public_count_published_cases",
        {"search": "procurement"},
    )


@pytest.mark.django_db
def test_public_chat_broad_count_does_not_pass_question_as_search(public_chat_config):
    with (
        patch(
            "public_chat.views.understand_question",
            return_value=QueryPlan(
                route="case_count",
                retrieval_query="how many published cases are there in jawafdehi?",
                reason="count",
                tool_name="public_count_published_cases",
                classifier_source="semantic",
                confidence=0.95,
            ),
        ),
        patch(
            "public_chat.views.PublicChatMCPClient.call_tool",
            return_value={
                "published_count": 5,
                "count_scope": "published_only",
                "results": [],
            },
        ) as mcp_call,
        patch(
            "public_chat.views.generate_answer",
            return_value="There are 5 published cases in Jawafdehi.",
        ),
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={
                "question": "how many published cases are there in jawafdehi?",
                "session_id": "session-broad-count",
            },
            format="json",
        )

    assert response.status_code == 200
    mcp_call.assert_called_once_with("public_count_published_cases", {})


@pytest.mark.django_db
def test_public_chat_rejects_unverified_count_payload(public_chat_config):
    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question("How many procurement cases are published?"),
        ),
        patch(
            "public_chat.views.PublicChatMCPClient.call_tool",
            return_value={"count": 99, "results": []},
        ),
        patch("public_chat.views.generate_answer") as llm_call,
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={
                "question": "How many procurement cases are published?",
                "session_id": "session-count-unverified",
            },
            format="json",
        )

    assert response.status_code == 503
    assert response.data == {
        "detail": "Public chat retrieval failed.",
        "error": "retrieval_failed",
    }
    llm_call.assert_not_called()


@pytest.mark.django_db
def test_public_chat_can_get_specific_published_case(public_chat_config):
    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question("Show case case-42 details"),
        ),
        patch(
            "public_chat.views.PublicChatMCPClient.call_tool",
            return_value=published_case_payload(),
        ) as mcp_call,
        patch(
            "public_chat.views.generate_answer",
            return_value="Here are the supported public details for case 42.",
        ) as llm_call,
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={"question": "Show case case-42 details", "session_id": "session-a"},
            format="json",
        )

    assert response.status_code == 200
    assert response.data["related_cases"][0]["case_id"] == "case-42"
    assert response.data["related_cases"][0]["url"] == "/case/42"
    mcp_call.assert_called_once_with(
        "public_get_published_case",
        {"case_id": "case-42", "fetch_sources": True},
    )
    llm_call.assert_called_once()


@pytest.mark.django_db
def test_public_chat_rejects_malformed_case_payload_before_answer_llm(
    public_chat_config,
):
    malformed_case = published_case_payload()
    malformed_case.pop("id")

    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question("Show case case-42 details"),
        ),
        patch(
            "public_chat.views.PublicChatMCPClient.call_tool",
            return_value=malformed_case,
        ),
        patch("public_chat.views.generate_answer") as llm_call,
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={"question": "Show case case-42 details", "session_id": "session-a"},
            format="json",
        )

    assert response.status_code == 503
    assert response.data == {
        "detail": "Public chat retrieval failed.",
        "error": "retrieval_failed",
    }
    llm_call.assert_not_called()


@pytest.mark.django_db
def test_public_chat_case_list_calls_mcp_without_search_term(public_chat_config):
    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question(
                "What are the current published Jawafdehi cases?"
            ),
        ),
        patch(
            "public_chat.views.PublicChatMCPClient.call_tool",
            return_value={"count": 1, "results": [published_case_payload()]},
        ) as mcp_call,
        patch(
            "public_chat.views.generate_answer",
            return_value="There is one current published case.",
        ),
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={
                "question": "What are the current published Jawafdehi cases?",
                "session_id": "session-list",
            },
            format="json",
        )

    assert response.status_code == 200
    assert response.data["sources"][0]["type"] == "case"
    mcp_call.assert_called_once_with("public_search_published_cases", {"page": 1})


@pytest.mark.django_db
def test_public_chat_sanitizes_entity_payloads_from_mcp(public_chat_config):
    captured = {}

    def fake_generate_answer(config, prompt):
        captured["prompt"] = prompt
        return "Example Person is a public entity related to procurement."

    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question("Show person entities related to procurement"),
        ),
        patch(
            "public_chat.views.PublicChatMCPClient.call_tool",
            return_value={
                "count": 1,
                "results": [
                    {
                        "id": 1,
                        "nes_id": "entity:person/example",
                        "display_name": "Example Person",
                        "type": "person",
                        "notes": "private entity note",
                        "moderation_status": "internal",
                        "related_cases": [
                            {
                                "case_id": "case-1",
                                "state": "PUBLISHED",
                                "relation_type": "accused",
                                "notes": "private relation note",
                            },
                            {
                                "case_id": "case-2",
                                "state": "IN_REVIEW",
                                "relation_type": "witness",
                            },
                        ],
                    }
                ],
            },
        ),
        patch("public_chat.views.generate_answer", side_effect=fake_generate_answer),
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={
                "question": "Show person entities related to procurement",
                "session_id": "session-entity",
            },
            format="json",
        )

    assert response.status_code == 200
    assert "Example Person" in captured["prompt"]
    assert "private entity note" not in captured["prompt"]
    assert "moderation_status" not in captured["prompt"]
    assert "private relation note" not in captured["prompt"]
    assert "case-2" not in captured["prompt"]


@pytest.mark.django_db
def test_public_chat_strips_internal_source_urls_from_mcp(public_chat_config):
    captured = {}

    def fake_generate_answer(config, prompt):
        captured["prompt"] = prompt
        return "The published case includes public evidence."

    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question(
                "Tell me about procurement cases", default_to_case_search=True
            ),
        ),
        patch(
            "public_chat.views.PublicChatMCPClient.call_tool",
            return_value={
                "count": 1,
                "results": [
                    published_case_payload(
                        evidence=[
                            {
                                "source_id": "source-1",
                                "description": "Internal source attachment",
                                "source": {
                                    "source_id": "source-1",
                                    "title": "Internal source",
                                    "description": "Should not expose internal URLs",
                                    "source_type": "document",
                                    "url": [
                                        "https://api.jawafdehi.org/api/sources/source-1/file",
                                        "https://jawafdehi.org/sources/source-1/file",
                                        "https://jawafdehi.org/media/source-1/file.pdf",
                                        "https://example.org/public-report.pdf",
                                    ],
                                },
                            }
                        ]
                    )
                ],
            },
        ),
        patch("public_chat.views.generate_answer", side_effect=fake_generate_answer),
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={"question": "Tell me about procurement cases", "session_id": "session-a"},
            format="json",
        )

    assert response.status_code == 200
    prompt_text = captured["prompt"].as_text()
    assert "https://example.org/public-report.pdf" in prompt_text
    assert "/api/sources/" not in prompt_text
    assert "jawafdehi.org/sources/" not in prompt_text
    assert "/media/" not in prompt_text


@pytest.mark.django_db
def test_quota_blocks_before_mcp_and_llm_even_after_session_reset(public_chat_config):
    public_chat_config.quota_limit = 1
    public_chat_config.save()

    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question(
                "procurement cases", default_to_case_search=True
            ),
        ),
        patch(
            "public_chat.views.PublicChatMCPClient.call_tool",
            return_value={"count": 1, "results": [published_case_payload()]},
        ) as mcp_call,
        patch(
            "public_chat.views.generate_answer", return_value="Supported answer"
        ) as llm_call,
    ):
        first = APIClient().post(
            PUBLIC_CHAT_URL,
            data={"question": "procurement cases", "session_id": "session-a"},
            format="json",
            REMOTE_ADDR="203.0.113.10",
        )
        second = APIClient().post(
            PUBLIC_CHAT_URL,
            data={"question": "procurement cases", "session_id": "session-b"},
            format="json",
            REMOTE_ADDR="203.0.113.10",
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.data["error"] == "quota_exceeded"
    assert mcp_call.call_count == 1
    assert llm_call.call_count == 1


@pytest.mark.django_db
def test_rag_disabled_refuses_before_mcp_or_answer_llm(public_chat_config):
    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question(
                "In the 2078 annual report, how many cases were registered?"
            ),
        ),
        patch("public_chat.views.PublicChatMCPClient.call_tool") as mcp_call,
        patch("public_chat.views.generate_answer") as llm_call,
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={
                "question": "In the 2078 annual report, how many cases were registered?"
            },
            format="json",
        )

    assert response.status_code == 200
    assert "public knowledge index" in response.data["answer_text"]
    assert response.data["sources"] == []
    mcp_call.assert_not_called()
    llm_call.assert_not_called()


@pytest.mark.django_db
def test_rag_enabled_without_chunks_refuses_before_mcp_or_answer_llm(
    public_chat_config,
):
    public_chat_config.knowledge_rag_enabled = True
    public_chat_config.save()

    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question(
                "In the 2079 annual report, how many cases were registered?"
            ),
        ),
        patch("public_chat.views.PublicChatMCPClient.call_tool") as mcp_call,
        patch("public_chat.views.generate_answer") as llm_call,
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={
                "question": "In the 2079 annual report, how many cases were registered?"
            },
            format="json",
        )

    assert response.status_code == 200
    assert (
        "could not find configured public Jawafdehi records"
        in response.data["answer_text"]
    )
    assert response.data["sources"] == []
    mcp_call.assert_not_called()
    llm_call.assert_not_called()


@pytest.mark.django_db
def test_public_chat_knowledge_rag_uses_configured_public_chunks(public_chat_config):
    collection = KnowledgeCollection.objects.create(
        name="annual_reports",
        display_name="Annual Reports",
        access_level=AccessLevel.PUBLIC,
    )
    source = KnowledgeSource.objects.create(
        collection=collection,
        title="Annual Report 2079",
        source_type="annual_report",
        source_url="https://jawafdehi.org/reports/2079.pdf",
        storage_path="gs://private-bucket/reports/2079.pdf",
        metadata={"internal_key": "do-not-expose"},
        access_level=AccessLevel.PUBLIC,
    )
    KnowledgeChunk.objects.create(
        source=source,
        chunk_index=0,
        text="In fiscal year 2079, the report says 120 cases were registered by type.",
        page_start=21,
        page_end=21,
        section_title="Case registration by type",
        metadata={"internal_chunk_key": "do-not-expose"},
        content_hash="knowledge-public-chat-2079",
    )
    public_chat_config.knowledge_rag_enabled = True
    public_chat_config.save()
    public_chat_config.knowledge_collections.add(collection)
    captured = {}

    def fake_generate_answer(config, prompt):
        captured["prompt"] = prompt
        return "The 2079 report says 120 cases were registered by type."

    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question(
                "In the 2079 annual report, how many cases were registered by type?"
            ),
        ),
        patch("public_chat.views.PublicChatMCPClient.call_tool") as mcp_call,
        patch("public_chat.views.generate_answer", side_effect=fake_generate_answer),
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={
                "question": "In the 2079 annual report, how many cases were registered by type?",
                "session_id": "session-rag",
            },
            format="json",
        )

    assert response.status_code == 200
    assert "knowledge_chunks" in captured["prompt"]
    assert "retrieval_mode" in captured["prompt"]
    assert "gs://private-bucket" not in captured["prompt"]
    assert "internal_key" not in captured["prompt"]
    assert "internal_chunk_key" not in captured["prompt"]
    assert (
        response.data["sources"][0]["url"] == "https://jawafdehi.org/reports/2079.pdf"
    )
    assert response.data["sources"][0]["page_start"] == 21
    assert response.data["sources"][0]["retrieval_mode"] == "lexical"
    assert response.data["related_cases"] == []
    mcp_call.assert_not_called()


@pytest.mark.django_db
def test_public_chat_rag_skill_triggers_and_scopes_retrieval(public_chat_config):
    selected_collection = KnowledgeCollection.objects.create(
        name="ciaa_annual_reports",
        display_name="CIAA Annual Reports",
        access_level=AccessLevel.PUBLIC,
    )
    selected_source = KnowledgeSource.objects.create(
        collection=selected_collection,
        title="CIAA Annual Report 2081/82",
        source_type="annual_report",
        source_url="https://jawafdehi.org/reports/2081-82.pdf",
        access_level=AccessLevel.PUBLIC,
    )
    KnowledgeChunk.objects.create(
        source=selected_source,
        chunk_index=0,
        text="The CIAA annual report 2081/82 states that 135 cases were registered.",
        section_title="Registered cases",
        content_hash="ciaa-annual-report-2081-82",
    )
    other_collection = KnowledgeCollection.objects.create(
        name="methodology",
        display_name="Methodology",
        access_level=AccessLevel.PUBLIC,
    )
    other_source = KnowledgeSource.objects.create(
        collection=other_collection,
        title="Public Methodology",
        source_type="methodology",
        source_url="https://jawafdehi.org/methodology",
        access_level=AccessLevel.PUBLIC,
    )
    KnowledgeChunk.objects.create(
        source=other_source,
        chunk_index=0,
        text="The methodology document says scoring uses five review stages.",
        content_hash="methodology-review-stages",
    )
    global_skill = Skill.objects.create(
        name="public-answer-style",
        display_name="Public Answer Style",
        description="Global answer style",
        content="Keep answers concise for public readers.",
    )
    selected_skill = Skill.objects.create(
        name="ciaa-annual-reports",
        display_name="CIAA Annual Reports",
        description="Annual report skill",
        content="Use annual report chunks and cite exact pages when available.",
    )
    other_skill = Skill.objects.create(
        name="methodology-documents",
        display_name="Methodology Documents",
        description="Methodology skill",
        content="This methodology skill must not load for annual report questions.",
    )
    selected_profile = RAGSkillProfile.objects.create(
        name="ciaa-annual-reports",
        display_name="CIAA Annual Reports",
        description="Annual report profile",
        skill=selected_skill,
        trigger_keywords=["ciaa annual report", "2081/82"],
        max_results=2,
        priority=10,
    )
    selected_profile.collections.add(selected_collection)
    other_profile = RAGSkillProfile.objects.create(
        name="methodology-documents",
        display_name="Methodology Documents",
        description="Methodology profile",
        skill=other_skill,
        trigger_keywords=["methodology"],
        max_results=2,
        priority=20,
    )
    other_profile.collections.add(other_collection)
    public_chat_config.knowledge_rag_enabled = True
    public_chat_config.save()
    public_chat_config.knowledge_collections.add(other_collection)
    public_chat_config.rag_skill_profiles.add(selected_profile, other_profile)
    public_chat_config.prompt.skills.add(global_skill, selected_skill, other_skill)
    captured = {}

    def fake_generate_answer(config, prompt):
        captured["prompt"] = prompt
        return "The 2081/82 CIAA annual report says 135 cases were registered."

    with (
        patch("public_chat.views.PublicChatMCPClient.call_tool") as mcp_call,
        patch("public_chat.views.generate_answer", side_effect=fake_generate_answer),
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={
                "question": "According to the CIAA annual report 2081/82, how many cases were registered?",
                "session_id": "session-rag-skill",
            },
            format="json",
        )

    prompt_text = captured["prompt"].as_text()
    assert response.status_code == 200
    assert '"classifier_source": "rag_skill"' in prompt_text
    assert "rag_skill" in prompt_text
    assert "ciaa-annual-reports" in prompt_text
    assert "135 cases were registered" in prompt_text
    assert "scoring uses five review stages" not in prompt_text
    assert "Keep answers concise for public readers" in prompt_text
    assert "Use annual report chunks" in prompt_text
    assert "methodology skill must not load" not in prompt_text
    assert response.data["sources"][0]["title"] == "CIAA Annual Report 2081/82"
    assert response.data["sources"][0]["type"] == "annual_report"
    mcp_call.assert_not_called()


@pytest.mark.django_db
def test_public_chat_knowledge_sources_return_public_citation_metadata(
    public_chat_config,
):
    collection = KnowledgeCollection.objects.create(
        name="annual_reports",
        display_name="Annual Reports",
        access_level=AccessLevel.PUBLIC,
    )
    source = KnowledgeSource.objects.create(
        collection=collection,
        title="Internal storage title",
        source_type="annual_report",
        storage_path="gs://private-bucket/reports/2079.pdf",
        metadata={
            "public_citation": {
                "title": "Annual Report 2079",
                "identifier": "annual-report-2079",
                "publisher": "CIAA",
                "publication_date": "2080-01-01",
            }
        },
        access_level=AccessLevel.PUBLIC,
    )
    KnowledgeChunk.objects.create(
        source=source,
        chunk_index=0,
        text="In fiscal year 2079, the annual report says 120 cases were registered.",
        page_start=21,
        page_end=22,
        section_title="Case registration by type",
        content_hash="knowledge-public-citation-2079",
    )
    public_chat_config.knowledge_rag_enabled = True
    public_chat_config.save()
    public_chat_config.knowledge_collections.add(collection)

    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question(
                "In the 2079 annual report, how many cases were registered?"
            ),
        ),
        patch("public_chat.views.PublicChatMCPClient.call_tool") as mcp_call,
        patch(
            "public_chat.views.generate_answer",
            return_value="The 2079 report says 120 cases were registered.",
        ),
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={
                "question": "In the 2079 annual report, how many cases were registered?",
                "session_id": "session-rag-citation",
            },
            format="json",
        )

    assert response.status_code == 200
    source_response = response.data["sources"][0]
    assert source_response["title"] == "Annual Report 2079"
    assert source_response["url"] == ""
    assert source_response["citation_identifier"] == "annual-report-2079"
    assert source_response["citation_publisher"] == "CIAA"
    assert source_response["citation_publication_date"] == "2080-01-01"
    assert "gs://private-bucket" not in str(source_response)
    mcp_call.assert_not_called()


@pytest.mark.django_db
def test_public_chat_uncertain_route_refuses_before_retrieval(public_chat_config):
    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question(
                "Tell me about that issue", default_to_case_search=False
            ),
        ),
        patch("public_chat.views.PublicChatMCPClient.call_tool") as mcp_call,
        patch("public_chat.views.generate_answer") as llm_call,
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={"question": "Tell me about that issue", "session_id": "session-a"},
            format="json",
        )

    assert response.status_code == 200
    assert "could not confidently choose" in response.data["answer_text"]
    assert response.data["sources"] == []
    mcp_call.assert_not_called()
    llm_call.assert_not_called()


@pytest.mark.django_db
def test_non_published_mcp_output_is_rejected_defensively(public_chat_config):
    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question(
                "procurement cases", default_to_case_search=True
            ),
        ),
        patch(
            "public_chat.views.PublicChatMCPClient.call_tool",
            return_value={
                "count": 1,
                "results": [published_case_payload(state="IN_REVIEW")],
            },
        ),
        patch("public_chat.views.generate_answer") as llm_call,
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={"question": "procurement cases", "session_id": "session-a"},
            format="json",
        )

    assert response.status_code == 503
    assert response.data == {
        "detail": "Public chat retrieval failed.",
        "error": "retrieval_failed",
    }
    llm_call.assert_not_called()


@pytest.mark.django_db
def test_public_chat_logs_retrieval_observability(public_chat_config, caplog):
    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question(
                "What are the current published Jawafdehi cases?"
            ),
        ),
        patch(
            "public_chat.views.PublicChatMCPClient.call_tool",
            return_value={"count": 1, "results": [published_case_payload()]},
        ),
        patch(
            "public_chat.views.generate_answer",
            return_value="There is one current published case.",
        ),
        caplog.at_level(logging.INFO, logger="public_chat.views"),
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={
                "question": "What are the current published Jawafdehi cases?",
                "session_id": "session-observability",
            },
            format="json",
        )

    assert response.status_code == 200
    assert "public_chat_retrieval_completed" in caplog.text
    assert "result_count=1" in caplog.text
    assert "mcp_tool=public_search_published_cases" in caplog.text


@pytest.mark.django_db
def test_mcp_failure_returns_clean_503(public_chat_config):
    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question(
                "procurement cases", default_to_case_search=True
            ),
        ),
        patch(
            "public_chat.views.PublicChatMCPClient.call_tool",
            side_effect=PublicChatMCPError("Error accessing public cases API: timeout"),
        ),
        patch("public_chat.views.generate_answer") as llm_call,
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={"question": "procurement cases", "session_id": "session-a"},
            format="json",
        )

    assert response.status_code == 503
    assert response.data == {
        "detail": "Public chat retrieval failed.",
        "error": "retrieval_failed",
    }
    llm_call.assert_not_called()


@pytest.mark.django_db
@override_settings(DEBUG=False, PUBLIC_CHAT_MCP_SERVERS={})
def test_missing_production_mcp_config_returns_503_before_quota(public_chat_config):
    with (
        patch(
            "public_chat.views.understand_question",
            return_value=route_question(
                "procurement cases", default_to_case_search=True
            ),
        ),
        patch("public_chat.views.PublicChatMCPClient.call_tool") as mcp_call,
    ):
        response = APIClient().post(
            PUBLIC_CHAT_URL,
            data={"question": "procurement cases", "session_id": "session-a"},
            format="json",
            REMOTE_ADDR="203.0.113.30",
        )

    assert response.status_code == 503
    assert response.data == {
        "detail": "Public chat retrieval failed.",
        "error": "retrieval_failed",
    }
    mcp_call.assert_not_called()


@pytest.mark.django_db
@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "test-default-cache",
        },
        QUOTA_CACHE_NAME: {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "test-public-chat-quota-cache",
        },
    }
)
def test_quota_uses_named_public_chat_cache(public_chat_config):
    class Request:
        META = {"REMOTE_ADDR": "203.0.113.40"}

    public_chat_config.quota_scope = "ip"
    public_chat_config.save()
    caches["default"].clear()
    caches[QUOTA_CACHE_NAME].clear()

    first = check_and_increment_quota(public_chat_config, Request(), "session-a")
    caches["default"].clear()
    second = check_and_increment_quota(public_chat_config, Request(), "session-a")
    caches[QUOTA_CACHE_NAME].clear()
    reset = check_and_increment_quota(public_chat_config, Request(), "session-a")

    assert first["used"] == 1
    assert second["used"] == 2
    assert reset["used"] == 1
