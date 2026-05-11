from __future__ import annotations

import logging
import uuid
import ipaddress
import re
from typing import Any
import json
from urllib.parse import urlparse

from django.conf import settings
from django.http import StreamingHttpResponse
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from caseworker.models import PublicChatConfig
from knowledge.models import AccessLevel
from knowledge.retrieval import KnowledgeAccessContext, KnowledgeRetriever

from .citation_validator import filter_public_sources
from .llm import (
    PublicChatAnswer,
    PublicChatLLMError,
    build_public_chat_messages,
    generate_answer,
)
from .mcp_client import PublicChatMCPClient, PublicChatMCPError
from .query_understanding import understand_question
from .quota import check_and_increment_quota
from .response_builder import (
    UNSUPPORTED_RAG_MESSAGE,
    build_case_source,
    build_knowledge_source,
    build_related_case,
    refusal_response,
)
from .routing import PUBLIC_CHAT_MCP_TOOLS, RouteDecision, normalize_search
from .serializers import (
    PublicChatRequestSerializer,
    PublicChatResponseSerializer,
    PublicEvidenceCaseSerializer,
    PublicEvidenceEntitySerializer,
)

logger = logging.getLogger(__name__)


PUBLIC_ENTITY_FIELDS = {"id", "nes_id", "display_name", "type"}
PUBLIC_RELATED_CASE_FIELDS = {"case_id", "relation_type"}
PUBLIC_CASE_FIELDS = {
    "id",
    "case_id",
    "slug",
    "case_type",
    "state",
    "title",
    "short_description",
    "description",
    "key_allegations",
    "tags",
    "entities",
    "case_start_date",
    "case_end_date",
    "created_at",
    "updated_at",
    "evidence",
}


class PublicChatView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request):
        config = self._get_active_config()
        if config is None or not config.enabled:
            return Response(
                {"detail": "Public chat is not available right now."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        serializer = PublicChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        session_id = payload.get("session_id") or uuid.uuid4().hex
        question = payload["question"]
        if len(question) > config.max_question_chars:
            return Response(
                {
                    "detail": (
                        f"Question is too long. Maximum length is "
                        f"{config.max_question_chars} characters."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        quota = check_and_increment_quota(config, request, session_id)
        if not quota["allowed"]:
            return Response(
                {
                    "detail": "Public chat query limit reached.",
                    "error": "quota_exceeded",
                    "limit": quota["limit"],
                    "window_seconds": quota["window_seconds"],
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        history = self._bound_history(payload.get("history", []), config)
        language = payload.get("language") or "auto"
        decision = understand_question(config, question)

        if decision.route == "clarify":
            self._log_refusal(decision, "clarify")
            response_data = refusal_response(
                "I could not confidently choose the right public Jawafdehi source for that question. Please ask a more specific case, entity, or public document question.",
                session_id,
            )
            return Response(response_data)

        logger.info(
            "public_chat_route_selected route=%s classifier_source=%s confidence=%s",
            decision.route,
            decision.classifier_source,
            decision.confidence,
            extra={
                "route": decision.route,
                "classifier_source": decision.classifier_source,
                "confidence": decision.confidence,
            },
        )

        if decision.route == "document_rag" and not config.knowledge_rag_enabled:
            self._log_refusal(decision, "document_rag_disabled")
            response_data = refusal_response(UNSUPPORTED_RAG_MESSAGE, session_id)
            return Response(response_data)

        try:
            if decision.route == "document_rag":
                evidence = self._retrieve_knowledge_evidence(
                    decision, config, question=question
                )
            else:
                evidence = self._retrieve_mcp_evidence(decision, config)
        except PublicChatMCPError as exc:
            logger.warning(
                "public_chat_retrieval_failed route=%s error_type=%s",
                decision.route,
                type(exc).__name__,
                exc_info=True,
                extra={"route": decision.route, "error_type": type(exc).__name__},
            )
            return Response(
                {
                    "detail": "Public chat retrieval failed.",
                    "error": "retrieval_failed",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except Exception as exc:  # noqa: BLE001 - public errors must stay stable.
            logger.warning(
                "public_chat_retrieval_failed route=%s error_type=%s",
                decision.route,
                type(exc).__name__,
                exc_info=True,
                extra={"route": decision.route, "error_type": type(exc).__name__},
            )
            return Response(
                {
                    "detail": "Public chat retrieval failed.",
                    "error": "retrieval_failed",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        self._log_retrieval_completed(decision, evidence)
        has_count_evidence = _has_verified_count_evidence(evidence)
        if (
            not evidence.get("cases")
            and not evidence.get("entities")
            and not evidence.get("knowledge_chunks")
            and not has_count_evidence
        ):
            self._log_refusal(decision, "no_public_evidence", evidence=evidence)
            response_data = refusal_response(
                "I could not find configured public Jawafdehi records or knowledge sources that support an answer to that question.",
                session_id,
            )
            return Response(response_data)

        messages = build_public_chat_messages(
            config=config,
            question=question,
            history=history,
            evidence=evidence,
            language=language,
        )
        try:
            answer = _coerce_answer(generate_answer(config, messages), evidence)
        except PublicChatLLMError as exc:
            logger.warning(
                "public_chat_answer_generation_failed route=%s error_type=%s",
                decision.route,
                type(exc).__name__,
                exc_info=True,
                extra={"route": decision.route, "error_type": type(exc).__name__},
            )
            return Response(
                {
                    "detail": "Public chat answer generation failed.",
                    "error": "answer_generation_failed",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        sources = filter_public_sources(
            evidence.get("sources", []),
            allowed_source_refs=answer.source_refs,
            require_source_refs=not _has_verified_count_evidence(evidence),
        )
        related_cases = [build_related_case(case) for case in evidence.get("cases", [])]
        response_data = {
            "answer_text": answer.answer_text,
            "session_id": session_id,
            "sources": sources,
            "related_cases": related_cases,
            "follow_up_questions": answer.follow_up_questions[:3],
        }
        PublicChatResponseSerializer(data=response_data).is_valid(raise_exception=True)
        return Response(response_data)

    def _get_active_config(self):
        return (
            PublicChatConfig.objects.select_related(
                "prompt",
                "llm_provider",
                "classifier_llm_provider",
            )
            .prefetch_related("prompt__skills", "knowledge_collections")
            .prefetch_related(
                "rag_skill_profiles__collections", "rag_skill_profiles__skill"
            )
            .filter(is_active=True)
            .first()
        )

    def _bound_history(
        self, history: list[dict[str, str]], config
    ) -> list[dict[str, str]]:
        bounded = history[-config.max_history_turns * 2 :]
        total = 0
        result = []
        for item in reversed(bounded):
            content = item.get("content", "")
            total += len(content)
            if total > config.max_history_chars:
                break
            result.append(item)
        return list(reversed(result))

    def _retrieve_mcp_evidence(self, decision: RouteDecision, config) -> dict[str, Any]:
        if not decision.tool_name:
            raise PublicChatMCPError("No MCP tool is configured for this route")
        if config.max_tool_calls < 1:
            raise PublicChatMCPError("Public chat MCP tool budget is exhausted")
        if not settings.DEBUG and not settings.PUBLIC_CHAT_MCP_SERVERS:
            raise PublicChatMCPError("Public chat MCP server is not configured.")

        client = PublicChatMCPClient(allowed_tools=PUBLIC_CHAT_MCP_TOOLS)
        if decision.route == "case_get":
            data = client.call_tool(
                decision.tool_name,
                {"case_id": decision.case_identifier, "fetch_sources": True},
            )
            if data.get("state") != "PUBLISHED":
                raise PublicChatMCPError("MCP returned non-public case data")
            cases = [_sanitize_public_case(data)]
            sources = [build_case_source(case) for case in cases]
            return {
                "route": decision.route,
                "mcp_tool": decision.tool_name,
                "tool_calls": 1,
                "retrieval_query": decision.retrieval_query,
                "case_identifier": decision.case_identifier,
                "routing": self._routing_metadata(decision),
                "cases": cases,
                "entities": [],
                "sources": sources,
            }

        if decision.route == "case_count":
            tool_args: dict[str, Any] = {}
            normalized_count_search = normalize_search(decision.retrieval_query)
            if normalized_count_search:
                tool_args["search"] = normalized_count_search
            data = client.call_tool(decision.tool_name, tool_args)
            count = _extract_verified_public_count(data)
            raw_cases = list(data.get("results", []))
            _reject_non_public_cases(raw_cases)
            cases = [_sanitize_public_case(case) for case in raw_cases][
                : config.max_mcp_results
            ]
            sources = [build_case_source(case) for case in cases]
            return {
                "route": decision.route,
                "mcp_tool": decision.tool_name,
                "tool_calls": 1,
                "retrieval_query": decision.retrieval_query,
                "count": count,
                "count_scope": "published_only",
                "routing": self._routing_metadata(decision),
                "cases": cases,
                "entities": [],
                "sources": sources,
            }

        tool_args: dict[str, Any] = {"page": 1}
        if decision.route != "case_list" and decision.retrieval_query:
            tool_args["search"] = decision.retrieval_query
        data = client.call_tool(decision.tool_name, tool_args)
        if decision.route == "entity_search":
            entities = [
                _sanitize_public_entity(entity)
                for entity in list(data.get("results", []))[: config.max_mcp_results]
                if isinstance(entity, dict)
            ]
            return {
                "route": decision.route,
                "mcp_tool": decision.tool_name,
                "tool_calls": 1,
                "retrieval_query": decision.retrieval_query,
                "routing": self._routing_metadata(decision),
                "entities": entities,
                "cases": [],
                "sources": [],
            }

        raw_cases = list(data.get("results", []))
        _reject_non_public_cases(raw_cases)
        cases = [_sanitize_public_case(case) for case in raw_cases][
            : config.max_mcp_results
        ]
        sources = [build_case_source(case) for case in cases]

        return {
            "route": decision.route,
            "mcp_tool": decision.tool_name,
            "tool_calls": 1,
            "retrieval_query": decision.retrieval_query,
            "routing": self._routing_metadata(decision),
            "cases": cases,
            "entities": [],
            "sources": sources,
        }

    def _retrieve_knowledge_evidence(
        self, decision: RouteDecision, config, *, question: str
    ) -> dict[str, Any]:
        rag_skill = _matched_rag_skill(config, decision)
        if rag_skill is not None:
            collections = rag_skill.collections.filter(
                is_active=True,
                access_level=AccessLevel.PUBLIC,
            )
            max_results = rag_skill.max_results
        else:
            collections = config.knowledge_collections.filter(
                is_active=True,
                access_level=AccessLevel.PUBLIC,
            )
            max_results = config.max_knowledge_results

        retrieved = KnowledgeRetriever().retrieve(
            query=decision.retrieval_query or question,
            access_context=KnowledgeAccessContext.public_context(),
            collections=collections,
            max_results=max_results,
        )
        chunks = [item.as_public_evidence() for item in retrieved]
        skill_tool_chunks = self._retrieve_skill_tool_evidence(
            rag_skill, config, decision=decision
        )
        chunks.extend(skill_tool_chunks)
        sources = [build_knowledge_source(chunk) for chunk in chunks]
        retrieval_mode = (
            "hybrid"
            if any(chunk.get("retrieval_mode") == "hybrid" for chunk in chunks)
            else "skill_tool"
            if skill_tool_chunks and not retrieved
            else "lexical"
        )
        return {
            "route": decision.route,
            "retrieval_query": decision.retrieval_query,
            "retrieval_mode": retrieval_mode,
            "collection_ids": [collection.id for collection in collections],
            "rag_skill": (
                {
                    "name": rag_skill.name,
                    "display_name": rag_skill.display_name,
                    "requires_citations": rag_skill.requires_citations,
                    "instructions": rag_skill.skill.content
                    if rag_skill.skill_id and rag_skill.skill.is_active
                    else "",
                    "source_locations": _skill_source_locations(rag_skill),
                    "allowed_mcp_tools": _skill_allowed_mcp_tools(rag_skill),
                }
                if rag_skill is not None
                else None
            ),
            "tool_calls": len(skill_tool_chunks),
            "routing": self._routing_metadata(decision),
            "knowledge_chunks": chunks,
            "cases": [],
            "entities": [],
            "sources": sources,
        }

    def _retrieve_skill_tool_evidence(
        self, rag_skill, config, *, decision: RouteDecision
    ) -> list[dict[str, Any]]:
        if rag_skill is None or config.max_tool_calls < 1:
            return []
        allowed_tools = _skill_allowed_mcp_tools(rag_skill)
        source_locations = _skill_source_locations(rag_skill)
        if "convert_to_markdown" not in allowed_tools or not source_locations:
            return []
        if not settings.DEBUG and not settings.PUBLIC_CHAT_MCP_SERVERS:
            raise PublicChatMCPError("Public chat MCP server is not configured.")

        safe_urls = _safe_public_skill_urls(source_locations)[: config.max_tool_calls]
        client = PublicChatMCPClient(allowed_tools={"convert_to_markdown"})
        chunks: list[dict[str, Any]] = []
        for index, url in enumerate(safe_urls):
            page_args = _skill_source_page_args(rag_skill, url)
            if not page_args:
                toc_args = _skill_toc_page_args(rag_skill)
                if toc_args:
                    toc_data = client.call_tool("convert_to_markdown", {"uri": url} | toc_args)
                    toc_markdown = str(
                        toc_data.get("markdown") or toc_data.get("text") or ""
                    ).strip()
                    target_pages = _select_pages_from_toc(
                        toc_markdown,
                        decision=decision,
                        rag_skill=rag_skill,
                    )
                    if not target_pages:
                        continue
                    page_args = {"pages": target_pages}
                elif _is_pdf_url(url):
                    continue

            page_range = _page_range_from_args(page_args)
            tool_args = {"uri": url} | page_args
            data = client.call_tool("convert_to_markdown", tool_args)
            markdown = str(data.get("markdown") or data.get("text") or "").strip()
            if not markdown:
                continue
            page_start, page_end = _page_bounds(page_range)
            chunks.append(
                {
                    "chunk_id": f"skill-tool:{rag_skill.name}:{index}",
                    "source_id": f"skill-tool:{rag_skill.name}",
                    "document_id": url,
                    "source_title": rag_skill.display_name or rag_skill.name,
                    "source_type": "skill_source",
                    "source_url": url,
                    "section_title": (
                        f"Skill-fetched public source pages {page_range}"
                        if page_range
                        else "Skill-fetched public source"
                    ),
                    "table_title": "",
                    "page_start": page_start,
                    "page_end": page_end,
                    "text": markdown[: config.max_evidence_chars],
                    "score": 1.0,
                    "retrieval_mode": "skill_tool",
                    "public_citation": {
                        "title": rag_skill.display_name or rag_skill.name,
                        "url": url,
                        "identifier": url,
                    },
                    "routing_reason": decision.reason,
                }
            )
        return chunks

    @staticmethod
    def _routing_metadata(decision: RouteDecision) -> dict[str, Any]:
        return {
            "route": decision.route,
            "reason": decision.reason,
            "classifier_source": decision.classifier_source,
            "confidence": decision.confidence,
            "classifier_error": decision.classifier_error,
            "requires_document_citation": decision.requires_document_citation,
        }

    @staticmethod
    def _result_count(evidence: dict[str, Any]) -> int:
        if _has_verified_count_evidence(evidence):
            return 1
        return (
            len(evidence.get("cases", []))
            + len(evidence.get("entities", []))
            + len(evidence.get("knowledge_chunks", []))
        )

    def _log_retrieval_completed(
        self, decision: RouteDecision, evidence: dict[str, Any]
    ) -> None:
        result_count = self._result_count(evidence)
        logger.info(
            "public_chat_retrieval_completed route=%s classifier_source=%s "
            "retrieval_mode=%s result_count=%s mcp_tool=%s collection_ids=%s",
            decision.route,
            decision.classifier_source,
            evidence.get("retrieval_mode", "api"),
            result_count,
            evidence.get("mcp_tool"),
            evidence.get("collection_ids", []),
            extra={
                "route": decision.route,
                "classifier_source": decision.classifier_source,
                "retrieval_mode": evidence.get("retrieval_mode", "api"),
                "result_count": result_count,
                "mcp_tool": evidence.get("mcp_tool"),
                "collection_ids": evidence.get("collection_ids", []),
                "tool_calls": evidence.get("tool_calls", 0),
                "case_count_value": (
                    evidence.get("count")
                    if _has_verified_count_evidence(evidence)
                    else None
                ),
            },
        )

    @staticmethod
    def _log_refusal(
        decision: RouteDecision,
        refusal_reason: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        logger.info(
            "public_chat_refusal route=%s classifier_source=%s refusal_reason=%s",
            decision.route,
            decision.classifier_source,
            refusal_reason,
            extra={
                "route": decision.route,
                "classifier_source": decision.classifier_source,
                "refusal_reason": refusal_reason,
                "retrieval_mode": (evidence or {}).get("retrieval_mode"),
                "result_count": PublicChatView._result_count(evidence or {}),
            },
        )


class PublicChatStreamView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request):
        def events():
            yield _sse_event("status", {"stage": "accepted"})
            response = PublicChatView().post(request)
            payload = {
                "status": getattr(response, "status_code", 200),
                "data": getattr(response, "data", {}),
            }
            yield _sse_event("final", payload)

        response = StreamingHttpResponse(events(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response


def _sse_event(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _has_verified_count_evidence(evidence: dict[str, Any]) -> bool:
    return (
        evidence.get("route") == "case_count"
        and evidence.get("count_scope") == "published_only"
        and isinstance(evidence.get("count"), int)
        and evidence.get("count") >= 0
    )


def _coerce_answer(
    value: PublicChatAnswer | str | dict[str, Any], evidence: dict[str, Any]
) -> PublicChatAnswer:
    if isinstance(value, PublicChatAnswer):
        return value
    if isinstance(value, str):
        source_refs = [
            source.get("source_ref")
            for source in evidence.get("sources", [])
            if source.get("source_ref")
        ]
        return PublicChatAnswer(answer_text=value, source_refs=source_refs)
    return PublicChatAnswer.model_validate(value)


def _matched_rag_skill(config, decision: RouteDecision):
    if not getattr(decision, "rag_skill_name", ""):
        return None
    try:
        return config.rag_skill_profiles.get(
            name=decision.rag_skill_name,
            is_active=True,
        )
    except (
        Exception
    ):  # noqa: BLE001 - unmatched profiles fall back to config collections.
        return None


def _skill_source_locations(rag_skill) -> list[str]:
    metadata = getattr(rag_skill, "metadata", {}) or {}
    raw_locations = metadata.get("source_locations", [])
    if isinstance(raw_locations, str):
        raw_locations = [raw_locations]
    if not isinstance(raw_locations, list):
        return []
    return [item.strip() for item in raw_locations if isinstance(item, str) and item.strip()]


def _skill_allowed_mcp_tools(rag_skill) -> list[str]:
    metadata = getattr(rag_skill, "metadata", {}) or {}
    raw_tools = metadata.get("allowed_mcp_tools", [])
    if isinstance(raw_tools, str):
        raw_tools = [raw_tools]
    if not isinstance(raw_tools, list):
        return []
    return [item.strip() for item in raw_tools if isinstance(item, str) and item.strip()]


def _skill_source_page_args(rag_skill, url: str) -> dict[str, Any]:
    """Read optional per-source page range metadata for MCP conversion."""
    metadata = getattr(rag_skill, "metadata", {}) or {}
    page_ranges = metadata.get("source_page_ranges", {})
    if isinstance(page_ranges, dict):
        page_range = page_ranges.get(url)
        if isinstance(page_range, str) and page_range.strip():
            return {"pages": page_range.strip()}

    pages = metadata.get("pages") or metadata.get("page_range")
    if isinstance(pages, str) and pages.strip():
        return {"pages": pages.strip()}

    page_start = metadata.get("page_start")
    page_end = metadata.get("page_end")
    if page_start is not None:
        args = {"page_start": page_start}
        if page_end is not None:
            args["page_end"] = page_end
        return args

    return {}


def _skill_toc_page_args(rag_skill) -> dict[str, Any]:
    """Read optional TOC page metadata for a two-step PDF lookup."""
    metadata = getattr(rag_skill, "metadata", {}) or {}
    toc_pages = metadata.get("toc_pages") or metadata.get("toc_page_range")
    if isinstance(toc_pages, str) and toc_pages.strip():
        return {"pages": toc_pages.strip()}

    toc_page_start = metadata.get("toc_page_start")
    toc_page_end = metadata.get("toc_page_end")
    if toc_page_start is not None:
        args = {"page_start": toc_page_start}
        if toc_page_end is not None:
            args["page_end"] = toc_page_end
        return args

    return {}


def _select_pages_from_toc(
    markdown: str,
    *,
    decision: RouteDecision,
    rag_skill,
) -> str | None:
    """Find a configured section title in converted TOC text and return pages."""
    del decision  # Reserved for query-aware title selection in future revisions.
    if not markdown:
        return None

    metadata = getattr(rag_skill, "metadata", {}) or {}
    titles = _skill_toc_section_titles(rag_skill)
    if not titles:
        return None

    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    for title in titles:
        target = _normalized_toc_text(title)
        if not target:
            continue
        for index, line in enumerate(lines):
            candidates = [line]
            if index + 1 < len(lines):
                candidates.append(f"{line} {lines[index + 1]}")
            if index + 2 < len(lines):
                candidates.append(f"{line} {lines[index + 1]} {lines[index + 2]}")
            for candidate in candidates:
                if target not in _normalized_toc_text(candidate):
                    continue
                page = _last_page_number(candidate)
                if page is None and index + 1 < len(lines):
                    page = _last_page_number(lines[index + 1])
                if page is None:
                    continue
                return _page_range_for_toc_page(page, metadata)
        page = _find_page_near_toc_title(markdown, target)
        if page is not None:
            return _page_range_for_toc_page(page, metadata)
    return None


def _skill_toc_section_titles(rag_skill) -> list[str]:
    metadata = getattr(rag_skill, "metadata", {}) or {}
    raw_titles = metadata.get("toc_section_titles") or metadata.get("toc_section_title")
    if isinstance(raw_titles, str):
        raw_titles = [raw_titles]
    if not isinstance(raw_titles, list):
        return []
    return [item.strip() for item in raw_titles if isinstance(item, str) and item.strip()]


def _normalized_toc_text(value: str) -> str:
    normalized = _translate_nepali_digits(value).casefold()
    return re.sub(r"[\s\W_]+", "", normalized, flags=re.UNICODE)


def _find_page_near_toc_title(markdown: str, target: str) -> int | None:
    """Find a title in noisy TOC text and read the nearest following page number."""
    compact_text, offsets = _compact_text_with_offsets(markdown)
    if not compact_text:
        return None
    position = compact_text.find(target)
    if position < 0:
        return None
    raw_end = offsets[min(position + len(target) - 1, len(offsets) - 1)] + 1
    nearby_text = markdown[raw_end : raw_end + 300]
    return _first_page_number(nearby_text)


def _compact_text_with_offsets(value: str) -> tuple[str, list[int]]:
    translated = _translate_nepali_digits(value).casefold()
    chars: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(translated):
        if re.match(r"[\s\W_]", char, flags=re.UNICODE):
            continue
        chars.append(char)
        offsets.append(index)
    return "".join(chars), offsets


def _last_page_number(value: str) -> int | None:
    normalized = _translate_nepali_digits(value)
    numbers = re.findall(r"\d+", normalized)
    if not numbers:
        return None
    try:
        return int(numbers[-1])
    except ValueError:
        return None


def _first_page_number(value: str) -> int | None:
    normalized = _translate_nepali_digits(value)
    numbers = re.findall(r"\d+", normalized)
    if not numbers:
        return None
    try:
        return int(numbers[0])
    except ValueError:
        return None


def _page_range_for_toc_page(page: int, metadata: dict[str, Any]) -> str:
    page += _metadata_int(metadata, "toc_pdf_page_offset", 0)
    window = max(_metadata_int(metadata, "answer_page_window", 2), 0)
    end_page = page + window
    if end_page == page:
        return str(page)
    return f"{page}-{end_page}"


def _translate_nepali_digits(value: str) -> str:
    return value.translate(str.maketrans("०१२३४५६७८९", "0123456789"))


def _metadata_int(metadata: dict[str, Any], key: str, default: int) -> int:
    value = metadata.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _page_range_from_args(args: dict[str, Any]) -> str | None:
    pages = args.get("pages")
    if isinstance(pages, str) and pages.strip():
        return pages.strip()
    page_start = args.get("page_start")
    if page_start is None:
        return None
    page_end = args.get("page_end", page_start)
    if str(page_start) == str(page_end):
        return str(page_start)
    return f"{page_start}-{page_end}"


def _page_bounds(page_range: str | None) -> tuple[int | None, int | None]:
    if not page_range:
        return None, None
    numbers = re.findall(r"\d+", _translate_nepali_digits(page_range))
    if not numbers:
        return None, None
    start = int(numbers[0])
    end = int(numbers[-1])
    return start, end


def _is_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def _safe_public_skill_urls(urls: list[str]) -> list[str]:
    safe_urls = []
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        hostname = (parsed.hostname or "").strip().lower()
        if not hostname or hostname in {"localhost", "metadata.google.internal"}:
            continue
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            ip = None
        if ip and (ip.is_private or ip.is_loopback or ip.is_link_local):
            continue
        path = parsed.path
        if "/media/" in path or "/sources/" in path:
            continue
        if url not in safe_urls:
            safe_urls.append(url)
    return safe_urls


def _extract_verified_public_count(data: dict[str, Any]) -> int:
    if data.get("count_scope") != "published_only":
        raise PublicChatMCPError("MCP count response is not marked published-only")
    count = data.get("published_count")
    if not isinstance(count, int) or count < 0:
        raise PublicChatMCPError(
            "MCP count response is missing a valid published_count"
        )
    return count


def _reject_non_public_cases(cases: list[dict[str, Any]]) -> None:
    if any(
        not isinstance(case, dict) or case.get("state") != "PUBLISHED" for case in cases
    ):
        raise PublicChatMCPError("MCP returned non-public case data")


def _sanitize_public_case(case: dict[str, Any]) -> dict[str, Any]:
    sanitized = {
        field: case.get(field) for field in PUBLIC_CASE_FIELDS if field in case
    }
    sanitized["state"] = "PUBLISHED"
    sanitized["entities"] = [
        _sanitize_public_entity(entity)
        for entity in case.get("entities") or []
        if isinstance(entity, dict)
    ]
    if "evidence" in sanitized:
        sanitized["evidence"] = _sanitize_public_evidence(case.get("evidence") or [])
    serializer = PublicEvidenceCaseSerializer(data=sanitized)
    try:
        serializer.is_valid(raise_exception=True)
    except serializers.ValidationError as exc:
        raise PublicChatMCPError("MCP returned malformed public case data") from exc
    return dict(serializer.validated_data)


def _sanitize_public_entity(entity: dict[str, Any]) -> dict[str, Any]:
    sanitized = {
        field: entity.get(field) for field in PUBLIC_ENTITY_FIELDS if field in entity
    }
    related_cases = []
    for related in entity.get("related_cases") or []:
        if not isinstance(related, dict):
            continue
        if related.get("state") not in (None, "PUBLISHED"):
            continue
        related_cases.append(
            {
                field: related.get(field)
                for field in PUBLIC_RELATED_CASE_FIELDS
                if field in related
            }
        )
    if related_cases:
        sanitized["related_cases"] = related_cases
    serializer = PublicEvidenceEntitySerializer(data=sanitized)
    try:
        serializer.is_valid(raise_exception=True)
    except serializers.ValidationError as exc:
        raise PublicChatMCPError("MCP returned malformed public entity data") from exc
    return dict(serializer.validated_data)


def _sanitize_public_evidence(items: list[Any]) -> list[dict[str, Any]]:
    sanitized_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = {
            "source_id": item.get("source_id"),
            "description": item.get("description") or "",
        }
        source = item.get("source")
        if isinstance(source, dict):
            entry["source"] = _sanitize_public_source(source)
        sanitized_items.append(entry)
    return sanitized_items


def _sanitize_public_source(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": source.get("id"),
        "source_id": source.get("source_id"),
        "title": source.get("title") or "",
        "description": source.get("description") or "",
        "source_type": source.get("source_type"),
        "url": _safe_public_urls(source.get("url")),
        "publication_date": source.get("publication_date"),
    }


def _safe_public_urls(value: Any) -> list[str]:
    urls = value if isinstance(value, list) else [value] if value else []
    safe_urls = []
    for url in urls:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        path = urlparse(url).path
        if "/media/" in path or "/sources/" in path:
            continue
        if url not in safe_urls:
            safe_urls.append(url)
    return safe_urls
