from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config

OUT_OF_SCOPE_MESSAGE_EN = (
    "I'm not allowed to help with that. I can only help with Jawafdehi.org, "
    "public accountability, corruption cases, governance transparency, public "
    "entities, public case records, court case information, and public evidence."
)
OUT_OF_SCOPE_MESSAGE_NE = (
    "म त्यस विषयमा सहयोग गर्न अनुमति प्राप्त छैन। म Jawafdehi.org, सार्वजनिक "
    "जवाफदेहिता, भ्रष्टाचार सम्बन्धी मुद्दा, शासन पारदर्शिता, सार्वजनिक निकाय, "
    "अदालतका मुद्दा, र सार्वजनिक प्रमाणसम्बन्धी विषयमा मात्र सहयोग गर्न सक्छु।"
)

_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")

_DOMAIN_SIGNALS = {
    "jawafdehi",
    "जवाफदेही",
    "corruption",
    "भ्रष्टाचार",
    "accountability",
    "जवाफदेहिता",
    "governance",
    "transparency",
    "public entity",
    "public entities",
    "सार्वजनिक निकाय",
    "ciaa",
    "अख्तियार",
    "court case",
    "अदालत",
    "evidence",
    "source document",
    "annual report",
    "वार्षिक प्रतिवेदन",
    "case record",
    "public case",
    "case details",
    "this case",
    "linked evidence",
}

_OFF_DOMAIN_PATTERNS = (
    r"\b(write|debug|fix|implement|compile|refactor)\b.*\b(code|python|javascript|typescript|react|django|sql|api)\b",
    r"\b(code|python|javascript|typescript|react|django|leetcode|algorithm)\b",
    r"\b(recipe|cook|bake|restaurant|hotel|flight|travel|itinerary)\b",
    r"\b(weather|forecast|sports|score|movie|song|lyrics)\b",
    r"\bcrypto|bitcoin|stock|forex|trading|investment advice\b",
    r"\bdiagnose|symptom|medicine|medical|doctor|treatment\b",
    r"\bworkout|fitness|diet plan|weight loss\b",
    r"\bjoke|poem|story|novel|screenplay\b",
    r"\bpassword|phishing|malware|exploit|hack|bypass\b",
    r"\bresume|cover letter|dating|relationship advice\b",
)


def is_obviously_out_of_scope(question: str) -> bool:
    """Return True only for clear public-chat misuse, not broad routing."""

    normalized = " ".join((question or "").lower().split())
    if not normalized:
        return False
    if any(signal in normalized for signal in _DOMAIN_SIGNALS):
        return False
    return any(re.search(pattern, normalized) for pattern in _OFF_DOMAIN_PATTERNS)


def out_of_scope_message(language: str = "auto", question: str = "") -> str:
    if language == "ne" or (language == "auto" and _DEVANAGARI_RE.search(question)):
        return OUT_OF_SCOPE_MESSAGE_NE
    return OUT_OF_SCOPE_MESSAGE_EN


def out_of_scope_response(
    *, session_id: str, language: str, question: str
) -> dict[str, Any]:
    return {
        "answer_text": out_of_scope_message(language=language, question=question),
        "session_id": session_id,
        "sources": [],
        "related_cases": [],
        "follow_up_questions": [],
    }


@dataclass
class PublicChatGuardrailContext:
    question: str
    language: str = "auto"
    observed_sources: list[dict[str, Any]] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)

    def add_sources(self, sources: list[dict[str, Any]]) -> None:
        with self.lock:
            known = {_source_key(source) for source in self.observed_sources}
            for source in sources:
                key = _source_key(source)
                if key in known:
                    continue
                known.add(key)
                self.observed_sources.append(source)

    def filter_response_sources(self, response: Any) -> Any:
        if is_obviously_out_of_scope(self.question):
            message = out_of_scope_message(
                language=self.language, question=self.question
            )
            if hasattr(response, "answer_text"):
                response.answer_text = message
                response.sources = []
                response.related_cases = []
                response.follow_up_questions = []
            elif isinstance(response, dict):
                response["answer_text"] = message
                response["sources"] = []
                response["related_cases"] = []
                response["follow_up_questions"] = []
            return response

        verified = _verified_sources(
            _response_sources(response),
            self.observed_sources,
        )
        if hasattr(response, "sources"):
            response.sources = verified
        elif isinstance(response, dict):
            response["sources"] = verified
        return response


class PublicChatScopeGuardrailMiddleware(AgentMiddleware):
    """LangChain before-agent middleware for obvious off-domain requests."""

    def __init__(self, context: PublicChatGuardrailContext) -> None:
        super().__init__()
        self.context = context

    @hook_config(can_jump_to=["end"])
    def before_agent(self, state, runtime) -> dict[str, Any] | None:
        question = self.context.question or _latest_user_text(state.get("messages", []))
        if not is_obviously_out_of_scope(question):
            return None
        return {
            "messages": [
                {
                    "role": "assistant",
                    "content": out_of_scope_message(
                        language=self.context.language,
                        question=question,
                    ),
                }
            ],
            "jump_to": "end",
        }

    async def abefore_agent(self, state, runtime) -> dict[str, Any] | None:
        return self.before_agent(state, runtime)


class PublicChatToolObservationMiddleware(AgentMiddleware):
    """LangChain tool-call middleware that records sources returned by tools."""

    def __init__(self, context: PublicChatGuardrailContext) -> None:
        super().__init__()
        self.context = context

    def wrap_tool_call(self, request, handler):
        result = handler(request)
        self.context.add_sources(
            _extract_observed_sources(result, _tool_call_args(request))
        )
        return result

    async def awrap_tool_call(self, request, handler):
        result = await handler(request)
        self.context.add_sources(
            _extract_observed_sources(result, _tool_call_args(request))
        )
        return result


class PublicChatSourceVerificationMiddleware(AgentMiddleware):
    """LangChain after-agent middleware that removes unobserved final sources."""

    def __init__(self, context: PublicChatGuardrailContext) -> None:
        super().__init__()
        self.context = context

    def after_agent(self, state, runtime) -> dict[str, Any] | None:
        response = state.get("structured_response")
        if response is None:
            return None
        return {"structured_response": self.context.filter_response_sources(response)}

    async def aafter_agent(self, state, runtime) -> dict[str, Any] | None:
        return self.after_agent(state, runtime)


def _tool_call_args(request) -> dict[str, Any]:
    tool_call = getattr(request, "tool_call", None)
    if isinstance(tool_call, dict) and isinstance(tool_call.get("args"), dict):
        return tool_call["args"]
    return {}


def _latest_user_text(messages: list[Any]) -> str:
    for message in reversed(messages or []):
        if isinstance(message, dict):
            role = message.get("role") or message.get("type")
            if role in {"user", "human"}:
                return str(message.get("content") or "")
            continue
        if getattr(message, "type", "") in {"human", "user"}:
            return str(getattr(message, "content", "") or "")
    return ""


def _response_sources(response: Any) -> list[Any]:
    if hasattr(response, "sources"):
        return list(getattr(response, "sources") or [])
    if isinstance(response, dict):
        return list(response.get("sources") or [])
    return []


def _verified_sources(
    model_sources: list[Any], observed_sources: list[dict[str, Any]]
) -> list[Any]:
    if not model_sources or not observed_sources:
        return []

    observed_urls = {
        _normalize_url(source.get("url"))
        for source in observed_sources
        if source.get("url")
    }
    observed_titles = {
        str(source.get("title") or "").casefold()
        for source in observed_sources
        if source.get("title")
    }
    observed_source_ids = _observed_id_set(observed_sources, "source_id")
    observed_document_ids = _observed_id_set(observed_sources, "document_id")
    observed_chunk_ids = _observed_id_set(observed_sources, "chunk_id")

    verified = []
    for source in model_sources:
        url = _normalize_url(_source_value(source, "url"))
        title = str(_source_value(source, "title") or "").casefold()
        source_id = _source_value(source, "source_id")
        document_id = _source_value(source, "document_id")
        chunk_id = _source_value(source, "chunk_id")
        has_claimed_locator = any([url, source_id, document_id, chunk_id])
        locator_verified = (
            (url and url in observed_urls)
            or (source_id and str(source_id) in observed_source_ids)
            or (document_id and str(document_id) in observed_document_ids)
            or (chunk_id and str(chunk_id) in observed_chunk_ids)
        )
        title_verified = not has_claimed_locator and title and title in observed_titles
        if locator_verified or title_verified:
            verified.append(source)
    return verified


def _extract_observed_sources(
    result: Any, tool_args: dict[str, Any]
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for data in _coerce_result_data(result):
        _walk_for_sources(data, sources)

    uri = tool_args.get("uri")
    if isinstance(uri, str) and uri.startswith(("http://", "https://")):
        sources.append(
            {
                "url": uri,
                "title": uri,
                "page_start": tool_args.get("page_start"),
                "page_end": tool_args.get("page_end"),
            }
        )

    normalized = []
    for source in sources:
        public_source = _normalize_source(source)
        if public_source:
            normalized.append(public_source)
    return normalized


def _coerce_result_data(result: Any) -> list[Any]:
    if result is None:
        return []
    if isinstance(result, dict):
        return [result]
    if isinstance(result, str):
        try:
            return [json.loads(result)]
        except ValueError:
            return [{"text": result}]
    if isinstance(result, list):
        data: list[Any] = []
        for item in result:
            if isinstance(item, dict):
                data.extend(_coerce_result_data(item))
                continue
            text = getattr(item, "text", None)
            if isinstance(text, str):
                data.extend(_coerce_result_data(text))
        return data
    content = getattr(result, "content", None)
    if content is not None:
        return _coerce_result_data(content)
    text = getattr(result, "text", None)
    if isinstance(text, str):
        return _coerce_result_data(text)
    return []


def _walk_for_sources(value: Any, sources: list[dict[str, Any]]) -> None:
    if isinstance(value, list):
        for item in value:
            _walk_for_sources(item, sources)
        return
    if not isinstance(value, dict):
        return

    source = _normalize_source(value)
    if source:
        sources.append(source)

    citation = value.get("public_citation")
    if isinstance(citation, dict):
        citation_source = _normalize_source(
            {
                "title": citation.get("title"),
                "url": citation.get("url"),
                "citation_identifier": citation.get("identifier"),
                "citation_publisher": citation.get("publisher"),
                "citation_publication_date": citation.get("publication_date"),
                "source_id": value.get("source_id"),
                "document_id": value.get("document_id"),
                "chunk_id": value.get("chunk_id"),
                "page_start": value.get("page_start"),
                "page_end": value.get("page_end"),
            }
        )
        if citation_source:
            sources.append(citation_source)

    for item in value.values():
        _walk_for_sources(item, sources)


def _normalize_source(value: dict[str, Any]) -> dict[str, Any] | None:
    title = value.get("source_title") or value.get("title") or value.get("name") or ""
    url = value.get("source_url") or value.get("url") or ""
    if isinstance(url, list):
        url = next(
            (
                item
                for item in url
                if isinstance(item, str) and item.startswith(("http://", "https://"))
            ),
            "",
        )
    source_id = value.get("source_id")
    document_id = value.get("document_id")
    chunk_id = value.get("chunk_id")

    has_source_shape = any(
        item not in (None, "")
        for item in [title, url, source_id, document_id, chunk_id]
    ) and any(
        key in value
        for key in [
            "source_title",
            "source_url",
            "public_citation",
            "source_id",
            "document_id",
            "chunk_id",
            "source_type",
            "case_id",
            "case_type",
            "slug",
            "state",
            "page_start",
            "page_end",
            "url",
        ]
    )
    if not has_source_shape:
        return None

    return {
        "title": str(title or ""),
        "url": str(url or ""),
        "source_id": str(source_id) if source_id not in (None, "") else None,
        "document_id": str(document_id) if document_id not in (None, "") else None,
        "chunk_id": str(chunk_id) if chunk_id not in (None, "") else None,
        "page_start": value.get("page_start"),
        "page_end": value.get("page_end"),
        "citation_identifier": value.get("citation_identifier"),
        "citation_publisher": value.get("citation_publisher"),
        "citation_publication_date": value.get("citation_publication_date"),
    }


def _source_value(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _observed_id_set(sources: list[dict[str, Any]], key: str) -> set[str]:
    return {
        str(source.get(key)) for source in sources if source.get(key) not in (None, "")
    }


def _normalize_url(url: str | None) -> str:
    return (url or "").strip().rstrip("/")


def _source_key(source: dict[str, Any]) -> tuple:
    return (
        source.get("url") or "",
        source.get("source_id") or "",
        source.get("document_id") or "",
        source.get("chunk_id") or "",
        (source.get("title") or "").casefold(),
    )
