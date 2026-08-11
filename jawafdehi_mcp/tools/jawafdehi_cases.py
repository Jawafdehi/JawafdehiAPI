import json
import os
import re
import urllib.parse
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import structlog
from mcp.types import TextContent

from ..api_transport import embedded_api_client_kwargs
from ..path_safety import (
    encode_case_slug,
    encode_entity_ref,
    encode_material_parts,
)
from ..request_context import (
    get_forwarded_headers,
    get_local_service_token,
    is_local_stdio_transport,
    jawafdehi_bearer_token,
)
from .base import BaseTool, ToolExecutionResult, error_text
from .control_plane_schemas import JSON_PATCH_SCHEMA

logger = structlog.get_logger()

CASE_TYPE_VALUES = [
    "CORRUPTION",
    "BRIBERY",
    "FORGERY",
    "EMBEZZLEMENT",
    "ABUSE_OF_OFFICE",
    "MONEY_LAUNDERING",
    "ILLEGAL_PROPERTY",
    "EXAM_RIGGING",
    "TAX_EVASION",
    "BANKING_OFFENCE",
]
CASE_STATE_VALUES = ["DRAFT"]
MAX_MATERIAL_UPLOAD_BYTES = 100 * 1024 * 1024


def _nullable_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


TIMELINE_ITEM_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "title": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "date_bs": {"type": "string"},
        "end_date": {"type": "string"},
        "end_date_bs": {"type": "string"},
    },
    "required": ["date", "title"],
    "additionalProperties": False,
}
EVIDENCE_ITEM_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "material_iri": {"type": "string", "format": "uri"},
        "additional_details": _nullable_schema({"type": "string"}),
    },
    "required": ["material_iri"],
    "additionalProperties": False,
}
CASE_CREATE_PROPERTIES: dict[str, Any] = {
    "case_type": {
        "type": "string",
        "enum": CASE_TYPE_VALUES,
        "description": "Case type.",
    },
    "state": {
        "type": "string",
        "enum": CASE_STATE_VALUES,
        "default": "DRAFT",
        "description": "New cases must be created in DRAFT state.",
    },
    "title": {
        "type": "string",
        "maxLength": 200,
        "description": "Case title.",
    },
    "short_description": {"type": "string"},
    "description": {
        "type": "string",
        "description": "Full case description in Markdown.",
    },
    "thumbnail_url": {"type": "string", "maxLength": 500},
    "banner_url": {"type": "string", "maxLength": 500},
    "case_start_date": _nullable_schema({"type": "string", "format": "date"}),
    "case_end_date": _nullable_schema({"type": "string", "format": "date"}),
    "tags": {"type": "array", "items": {"type": "string"}},
    "key_allegations": {"type": "array", "items": {"type": "string"}},
    "timeline": {"type": "array", "items": TIMELINE_ITEM_INPUT_SCHEMA},
    "evidence": {"type": "array", "items": EVIDENCE_ITEM_INPUT_SCHEMA},
    "notes": {"type": "string"},
    "public_notes": {"type": "string"},
    "alleged_entities": {
        "type": "array",
        "items": {"type": "string", "format": "uri"},
    },
    "related_entities": {
        "type": "array",
        "items": {"type": "string", "format": "uri"},
    },
    "slug": _nullable_schema({"type": "string", "maxLength": 50}),
    "court_cases": _nullable_schema(
        {
            "type": "array",
            "items": {"type": "string", "format": "uri"},
        }
    ),
    "missing_details": _nullable_schema({"type": "string"}),
    "bigo": _nullable_schema(
        {
            "type": "integer",
            "minimum": -9223372036854775808,
            "maximum": 9223372036854775807,
        }
    ),
    # Editorial priority for the homepage "Featured Cases" order; higher floats
    # up, 0 is unranked. NOT nullable (unlike bigo) and a narrower 32-bit range —
    # the column is a NOT NULL IntegerField, not a nullable BigIntegerField.
    "weight": {
        "type": "integer",
        "minimum": -2147483648,
        "maximum": 2147483647,
    },
}
CASE_CREATE_FIELDS = tuple(CASE_CREATE_PROPERTIES)


def _get_jawafdehi_base_url() -> str:
    return os.getenv("JAWAFDEHI_API_BASE_URL", "https://api.jawafdehi.org").rstrip("/")


def _get_jawafdehi_api_token() -> str | None:
    return get_local_service_token()


def _has_upstream_auth() -> bool:
    """True if the request can authenticate to jawafdehi-api: a forwarded OIDC
    bearer (HTTP transport) or a service token (stdio/dev fallback)."""
    return bool(jawafdehi_bearer_token.get()) or bool(_get_jawafdehi_api_token())


def _get_auth_headers() -> dict[str, str]:
    """Return Authorization headers for upstream calls.

    Prefer the caller's forwarded OIDC bearer; fall back to the service token
    (stdio/dev), also sent as ``Bearer``. The unified platform is OIDC-only —
    the legacy DRF ``Token`` scheme is no longer honoured (2026-07 hard cut).
    HTTP requests never fall back to the process service token.
    """
    headers = get_forwarded_headers()
    if not headers:
        token = _get_jawafdehi_api_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


_NO_AUTH_MESSAGE = (
    "Authentication required: sign in (OIDC bearer) or set JAWAFDEHI_API_TOKEN."
)


def _json_text_content(payload: Any) -> list[TextContent]:
    return [
        TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))
    ]


def _json_error_content(payload: Any) -> ToolExecutionResult:
    return ToolExecutionResult(_json_text_content(payload), is_error=True)


def _error_text_content(message: str) -> ToolExecutionResult:
    return error_text(message)


def _mutation_timeout_content(operation: str) -> ToolExecutionResult:
    return _error_text_content(
        f"{operation} timed out; the operation's outcome is unknown. "
        "Verify current state before retrying."
    )


def _build_http_error_payload(response: httpx.Response, prefix: str) -> dict[str, Any]:
    try:
        details: Any = response.json()
    except ValueError:
        details = response.text

    payload = {
        "error": prefix,
        "status_code": response.status_code,
        "details": details,
    }
    if etag := _response_etag(response):
        payload["etag"] = etag
    return payload


def _response_etag(response: httpx.Response) -> str | None:
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    return headers.get("etag")


def _response_json_with_etag(response: httpx.Response) -> Any:
    payload = response.json()
    etag = _response_etag(response)
    if etag and isinstance(payload, dict):
        payload = {**payload, "etag": etag}
    return payload


def _flatten_lang_map(value: Any) -> str:
    """Flatten a unified-search language map ({en, ne}) to a single string.

    Prefers English, then Nepali, then any non-empty value. A plain string is
    returned as-is; anything else becomes "".
    """
    if isinstance(value, dict):
        for lang in ("en", "ne"):
            text = value.get(lang)
            if isinstance(text, str) and text:
                return text
        for text in value.values():
            if isinstance(text, str) and text:
                return text
        return ""
    return value if isinstance(value, str) else ""


def _slug_from_search_hit(hit: dict[str, Any]) -> str:
    """Extract a case slug from a unified-search hit.

    The /api/search/ result carries the slug inside ``api_url``
    (``/api/cases/<slug>/``) or ``url`` (``/case/<slug>``) — it has no bare
    ``slug`` field. get_jawafdehi_case needs the slug, so derive it here.
    """
    for key, pattern in (
        ("api_url", r"/api/cases/([^/]+)/?$"),
        ("url", r"/case/([^/]+)/?$"),
    ):
        value = hit.get(key)
        if isinstance(value, str):
            match = re.search(pattern, value)
            if match:
                return match.group(1)
    return ""


def _shape_case_search_hit(hit: dict[str, Any]) -> dict[str, Any]:
    """Map a raw /api/search/ case hit to the case shape the tool returns.

    Keeps a ``slug`` (for get_jawafdehi_case), a flattened title/snippet, and
    the case_type/date/url/score so the assistant can present and link results.
    """
    extra = hit.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    return {
        "slug": _slug_from_search_hit(hit),
        "title": _flatten_lang_map(hit.get("title")),
        "snippet": _flatten_lang_map(hit.get("snippet")),
        "case_type": extra.get("case_type"),
        "date": extra.get("date"),
        "url": hit.get("url"),
        "score": hit.get("score"),
    }


class SearchJawafdehiCasesTool(BaseTool):
    """Tool for searching Jawafdehi accountability cases."""

    @property
    def name(self) -> str:
        return "search_jawafdehi_cases"

    @property
    def description(self) -> str:
        return (
            "Search published Jawafdehi accountability cases by keywords or tags. "
            "Covers every case type (corruption, tax evasion, and others); pass "
            "case_type only to narrow the results to a single type."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": (
                        "Full-text search across title, description, "
                        "and key allegations."
                    ),
                },
                "tags": {
                    "type": "string",
                    "description": "Filter cases containing a specific tag.",
                },
                "case_type": {
                    "type": "string",
                    "description": (
                        "Optional. Restrict results to one case type "
                        "(e.g. CORRUPTION, TAX_EVASION, BRIBERY). Omit to search "
                        "across all case types."
                    ),
                },
                "page": {
                    "type": "integer",
                    "description": "Page number for pagination (defaults to 1).",
                    "default": 1,
                },
            },
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        # Query the unified OpenSearch plane (/api/search/), NOT the legacy
        # /api/cases/ list filter. That list filter is a plain ORM icontains
        # over title/description that misses Nepali-script titles — English
        # queries returned 0 results — and DRF 404s on an out-of-range page
        # (BB-03). /api/search/ is the bilingual, ranked search and returns an
        # empty page instead of a 404.
        query_params: dict[str, str] = {"type": "case"}

        search = arguments.get("search")
        if search:
            query_params["q"] = str(search)

        tags = arguments.get("tags")
        if tags:
            query_params["tags"] = str(tags)

        # Optional case-type filter — default is NO filter so every case type is
        # searchable. A previously hard-coded case_type=CORRUPTION silently hid
        # tax-evasion and other non-corruption cases from search and chat (BB-03).
        case_type = arguments.get("case_type")
        if case_type:
            query_params["case_type"] = str(case_type)

        if "page" in arguments:
            query_params["page"] = str(arguments["page"])

        query_string = urllib.parse.urlencode(query_params)
        base_url = _get_jawafdehi_base_url()
        url = f"{base_url.rstrip('/')}/api/search/?{query_string}"

        try:
            async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
                response = await client.get(
                    url, headers=_get_auth_headers(), timeout=30.0
                )
                if not response.is_success:
                    # Surface the API's own error body (status + detail) instead of
                    # a bare status string, so failures like an expired forwarded
                    # token ({"detail": "Token has expired."}) are diagnosable.
                    error_payload = _build_http_error_payload(
                        response, "Error accessing Jawafdehi search API."
                    )
                    # 401/403 is an expired/insufficient forwarded caller bearer —
                    # expected auth churn, not a server fault — so log at warning
                    # (below Sentry's ERROR threshold). 5xx and the rest stay error.
                    log = (
                        logger.warning
                        if response.status_code in (401, 403)
                        else logger.error
                    )
                    log(
                        "jawafdehi_search_http_error",
                        status_code=response.status_code,
                        details=error_payload.get("details"),
                    )
                    return _json_error_content(error_payload)
                data = response.json()

            # Defensive: a well-behaved /api/search/ returns a JSON object with a
            # list of dict hits, but never trust the shape blindly.
            if not isinstance(data, dict):
                data = {}
            raw_results = data.get("results")
            results = raw_results if isinstance(raw_results, list) else []
            payload = {
                "count": data.get("count"),
                "page": data.get("page"),
                "results": [
                    _shape_case_search_hit(hit)
                    for hit in results
                    if isinstance(hit, dict)
                ],
            }
            return _json_text_content(payload)
        except httpx.TimeoutException:
            logger.warning("jawafdehi_create_case_timeout")
            return _mutation_timeout_content("Creating the Jawafdehi case")
        except httpx.HTTPError as e:
            logger.error("jawafdehi_search_http_error", error=str(e))
            return _error_text_content(
                f"Error accessing Jawafdehi cases API: {str(e)}\n\n"
                f"Consider narrowing your search or checking parameters."
            )
        except Exception as e:
            logger.exception("jawafdehi_search_unexpected_error", error=str(e))
            return _error_text_content(f"Unexpected error: {str(e)}")


class GetJawafdehiCaseTool(BaseTool):
    """Tool for retrieving detailed info on a specific Jawafdehi case."""

    @property
    def name(self) -> str:
        return "get_jawafdehi_case"

    @property
    def description(self) -> str:
        return (
            "Retrieve detailed information about a specific Jawafdehi case "
            "(published or draft), including its allegations, evidence, timeline, "
            "and optionally its casework audit history. Each evidence entry is a reference into the "
            "Materials store — ``{material_iri, additional_details, material}`` — "
            "where ``material`` is the resolved material (display name, type, "
            "roled URLs), embedded by the API. All cases (including drafts) have "
            "auto-generated slugs. Use the 'slug' from search results for direct "
            "lookup. Set include_history for the separate casework-gated transition "
            "history."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": (
                        "The canonical URL slug of the case, typically from "
                        "search_jawafdehi_cases results."
                    ),
                },
                "include_history": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Include case-state transition history. This subresource "
                        "requires a casework-viewing role."
                    ),
                },
            },
            "required": ["slug"],
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        base_url = _get_jawafdehi_base_url()
        auth_headers = _get_auth_headers()

        slug = arguments.get("slug")

        if not slug or not isinstance(slug, str) or not slug.strip():
            return _error_text_content(
                "Error: 'slug' (string) is required. "
                "Use the 'slug' field from search_jawafdehi_cases results."
            )
        try:
            slug_path = encode_case_slug(slug)
        except ValueError as exc:
            return _error_text_content(f"Error: {exc}")
        case_url = f"{base_url.rstrip('/')}/api/cases/{slug_path}/"
        lookup_label = f"slug={slug.strip()}"

        # The case detail already embeds each evidence entry's resolved material
        # (cases own no documents — evidence is a CaseMaterialReference join, and
        # CaseDetailSerializer resolves the material inline). No separate
        # source-fetch loop is needed; the old /api/sources endpoint is gone.
        try:
            async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
                response = await client.get(
                    case_url, headers=auth_headers, timeout=30.0
                )
                if response.status_code == 404:
                    return _error_text_content(f"Case not found ({lookup_label}).")
                if not response.is_success:
                    # Surface the API's error body (e.g. an expired forwarded token
                    # → {"detail": "Token has expired."}) rather than a bare status.
                    error_payload = _build_http_error_payload(
                        response,
                        f"Error accessing Jawafdehi case API ({lookup_label}).",
                    )
                    # 401/403 is an expired/insufficient forwarded caller bearer —
                    # expected auth churn, not a server fault — so log at warning
                    # (below Sentry's ERROR threshold). 5xx and the rest stay error.
                    log = (
                        logger.warning
                        if response.status_code in (401, 403)
                        else logger.error
                    )
                    log(
                        "jawafdehi_get_case_http_error",
                        lookup_label=lookup_label,
                        status_code=response.status_code,
                        details=error_payload.get("details"),
                    )
                    return _json_error_content(error_payload)
                payload = _response_json_with_etag(response)
                if arguments.get("include_history") and isinstance(payload, dict):
                    history_response = await client.get(
                        f"{case_url}history/",
                        headers=auth_headers,
                        timeout=30.0,
                    )
                    if history_response.is_success:
                        payload["history"] = history_response.json()
                    else:
                        payload["history_error"] = _build_http_error_payload(
                            history_response,
                            "Error accessing Jawafdehi case history.",
                        )
                return _json_text_content(payload)
        except httpx.TimeoutException:
            logger.warning(
                "jawafdehi_patch_case_timeout",
                lookup_label=lookup_label,
            )
            return _mutation_timeout_content(
                f"Patching the Jawafdehi case ({lookup_label})"
            )
        except httpx.HTTPError as e:
            logger.error(
                "jawafdehi_get_case_http_error",
                lookup_label=lookup_label,
                error=str(e),
            )
            return _error_text_content(
                f"Error accessing Jawafdehi API ({lookup_label}): {str(e)}"
            )
        except Exception as e:
            logger.exception(
                "jawafdehi_get_case_unexpected_error",
                lookup_label=lookup_label,
                error=str(e),
            )
            return _error_text_content(f"Unexpected error: {str(e)}")


class CreateJawafdehiCaseTool(BaseTool):
    """Tool for creating a draft Jawafdehi case."""

    @property
    def name(self) -> str:
        return "create_jawafdehi_case"

    @property
    def description(self) -> str:
        return (
            "Create a draft Jawafdehi case using a simple authenticated interface. "
            "Requires a signed-in user with write access."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": deepcopy(CASE_CREATE_PROPERTIES),
            "required": ["title", "case_type"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        title = arguments.get("title")
        case_type = arguments.get("case_type")

        if not _has_upstream_auth():
            return _error_text_content(f"Error: {_NO_AUTH_MESSAGE}")

        if not title:
            return _error_text_content("Error: title is required")

        if not case_type:
            return _error_text_content("Error: case_type is required")

        payload = {
            field: arguments[field]
            for field in CASE_CREATE_FIELDS
            if field in arguments
        }

        url = f"{_get_jawafdehi_base_url()}/api/cases/"
        headers = _get_auth_headers()

        try:
            async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=30.0,
                )

                if response.is_success:
                    return _json_text_content(response.json())

                return _json_error_content(
                    _build_http_error_payload(
                        response, "Error creating Jawafdehi case via API."
                    )
                )
        except httpx.HTTPError as e:
            logger.error("jawafdehi_create_case_http_error", error=str(e))
            return _error_text_content(
                f"Error accessing Jawafdehi create API: {str(e)}"
            )
        except Exception as e:
            logger.exception("jawafdehi_create_case_unexpected_error", error=str(e))
            return _error_text_content(f"Unexpected error: {str(e)}")


class PatchJawafdehiCaseTool(BaseTool):
    """Tool for patching a Jawafdehi case with RFC 6902 operations."""

    @property
    def name(self) -> str:
        return "patch_jawafdehi_case"

    @property
    def description(self) -> str:
        return (
            "Patch a Jawafdehi case using raw RFC 6902 JSON Patch operations. "
            "Requires a signed-in user with write access. Use a slug (from "
            "search results) for direct lookup."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": (
                        "The URL slug of the case to patch. "
                        "Use the 'slug' field from search_jawafdehi_cases results."
                    ),
                },
                "operations": {
                    **deepcopy(JSON_PATCH_SCHEMA),
                    "description": "RFC 6902 JSON Patch operations. Use Markdown for /description values.",
                },
                "if_match": {
                    "type": "string",
                    "description": "ETag returned by get_jawafdehi_case.",
                },
                "transition_reason": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": (
                        "Optional audit reason when the patch changes case state."
                    ),
                },
            },
            "required": ["slug", "operations"],
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        operations = arguments.get("operations")
        slug = arguments.get("slug")

        if not _has_upstream_auth():
            return _error_text_content(f"Error: {_NO_AUTH_MESSAGE}")

        if not slug or not isinstance(slug, str) or not slug.strip():
            return _error_text_content(
                "Error: 'slug' (string) is required. "
                "Use the 'slug' field from search_jawafdehi_cases results."
            )

        if not isinstance(operations, list) or not operations:
            return _error_text_content(
                "Error: operations must be a non-empty JSON Patch array of "
                "operation objects."
            )

        try:
            slug_path = encode_case_slug(slug)
        except ValueError as exc:
            return _error_text_content(f"Error: {exc}")
        base_url = _get_jawafdehi_base_url()
        url = f"{base_url.rstrip('/')}/api/cases/{slug_path}/"
        lookup_label = f"slug={slug.strip()}"

        headers = _get_auth_headers()
        if arguments.get("if_match"):
            headers["If-Match"] = str(arguments["if_match"])
        if arguments.get("transition_reason"):
            headers["X-Transition-Reason"] = str(arguments["transition_reason"])

        try:
            async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
                response = await client.patch(
                    url,
                    json=operations,
                    headers=headers,
                    timeout=30.0,
                )

                if response.is_success:
                    return _json_text_content(_response_json_with_etag(response))

                return _json_error_content(
                    _build_http_error_payload(
                        response,
                        f"Error patching Jawafdehi case ({lookup_label}) via API.",
                    )
                )
        except httpx.HTTPError as e:
            logger.error(
                "jawafdehi_patch_case_http_error",
                lookup_label=lookup_label,
                error=str(e),
            )
            return _error_text_content(
                f"Error accessing Jawafdehi patch API ({lookup_label}): {str(e)}"
            )
        except Exception as e:
            logger.exception(
                "jawafdehi_patch_case_unexpected_error",
                lookup_label=lookup_label,
                error=str(e),
            )
            return _error_text_content(f"Unexpected error: {str(e)}")


class DeleteJawafdehiCaseTool(BaseTool):
    """Soft-delete a Jawafdehi case through the authoritative case API."""

    @property
    def name(self) -> str:
        return "delete_jawafdehi_case"

    @property
    def description(self) -> str:
        return (
            "Soft-delete a Jawafdehi case by transitioning it to CLOSED. "
            "The Django control plane enforces delete permission."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Canonical case slug.",
                }
            },
            "required": ["slug"],
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        if not _has_upstream_auth():
            return _error_text_content(f"Error: {_NO_AUTH_MESSAGE}")
        try:
            slug_path = encode_case_slug(arguments.get("slug", ""))
        except ValueError as exc:
            return _error_text_content(f"Error: {exc}")

        url = f"{_get_jawafdehi_base_url()}/api/cases/{slug_path}/"
        try:
            async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
                response = await client.delete(
                    url,
                    headers=_get_auth_headers(),
                    timeout=30.0,
                )
            if response.is_success:
                return _json_text_content(
                    {"success": True, "status_code": response.status_code}
                )
            return _json_error_content(
                _build_http_error_payload(
                    response,
                    "Error deleting Jawafdehi case via API.",
                )
            )
        except httpx.TimeoutException:
            logger.warning("jawafdehi_delete_case_timeout")
            return _mutation_timeout_content("Deleting the Jawafdehi case")
        except httpx.HTTPError as exc:
            logger.error("jawafdehi_delete_case_http_error", error=str(exc))
            return _error_text_content(f"Error accessing Jawafdehi delete API: {exc}")


class SubmitNESChangeTool(BaseTool):
    """Write an NES entity directly via the unified entity write plane.

    Post-unification (2026-07 hard cut) there is no NES *queue* endpoint
    (``/api/submit_nes_change`` and the ADD_NAME/CREATE_ENTITY/UPDATE_ENTITY
    NESQ actions are gone). Writes go straight to the entity store:
      * CREATE → ``POST /api/entities`` with a JSON-LD / authoring ``document``.
      * UPDATE → ``PATCH /api/entities/{ref}`` with RFC-6902 ``patch_ops``
        (add-a-name is just an ``add`` op to ``/name`` — no dedicated action).
    NES-contributor gated; the API enforces permissions and the ≥2-source held
    /published gate does NOT apply to direct API writes (they publish).
    """

    @property
    def name(self) -> str:
        return "submit_nes_change"

    @property
    def description(self) -> str:
        return (
            "Write an NES entity directly. Use action=CREATE with a JSON-LD "
            "'document' to create an entity, or action=UPDATE with 'ref' "
            "(the entity @id or prefix/slug) and RFC-6902 'patch_ops' to modify "
            "one (e.g. add a name: [{'op':'add','path':'/name/en','value':'...'}])."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["CREATE", "UPDATE"],
                    "description": "CREATE a new entity or UPDATE an existing one.",
                },
                "ref": {
                    "type": "string",
                    "description": (
                        "UPDATE only: the entity @id IRI or 'prefix/slug' path "
                        "(e.g. 'person/ram-chandra-poudel')."
                    ),
                },
                "document": {
                    "type": "object",
                    "description": (
                        "CREATE only: the JSON-LD / authoring entity document "
                        "(must carry @id or prefix+slug + @type)."
                    ),
                },
                "patch_ops": {
                    **deepcopy(JSON_PATCH_SCHEMA),
                    "description": (
                        "UPDATE only: RFC-6902 JSON Patch operations. Immutable "
                        "paths (@id/@type/@context/version) are rejected by the API."
                    ),
                },
                "change_description": {
                    "type": "string",
                    "description": "Human-readable summary of the change.",
                },
            },
            "required": ["action", "change_description"],
            "allOf": [
                {
                    "if": {
                        "properties": {"action": {"const": "CREATE"}},
                        "required": ["action"],
                    },
                    "then": {"required": ["document"]},
                },
                {
                    "if": {
                        "properties": {"action": {"const": "UPDATE"}},
                        "required": ["action"],
                    },
                    "then": {"required": ["ref", "patch_ops"]},
                },
            ],
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        if not _has_upstream_auth():
            return _error_text_content(f"Error: {_NO_AUTH_MESSAGE}")

        action = arguments.get("action")
        change_description = arguments.get("change_description")
        base_url = _get_jawafdehi_base_url()
        headers = _get_auth_headers()
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"

        if action == "CREATE":
            document = arguments.get("document")
            if not isinstance(document, dict):
                return _error_text_content(
                    "Error: 'document' (object) is required for action=CREATE."
                )
            url = f"{base_url}/api/entities"
            body = {**document, "change_description": change_description}
            method = "POST"
        elif action == "UPDATE":
            ref = arguments.get("ref")
            patch_ops = arguments.get("patch_ops")
            if not ref or not isinstance(patch_ops, list) or not patch_ops:
                return _error_text_content(
                    "Error: 'ref' and a non-empty 'patch_ops' array are required for "
                    "action=UPDATE."
                )
            try:
                ref_path = encode_entity_ref(ref)
            except ValueError as exc:
                return _error_text_content(f"Error: {exc}")
            url = f"{base_url}/api/entities/{ref_path}"
            body = {"patch_ops": patch_ops, "change_description": change_description}
            method = "PATCH"
        else:
            return _error_text_content(
                f"Error: unsupported action {action!r} (use CREATE or UPDATE)."
            )

        try:
            async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
                response = await client.request(
                    method, url, json=body, headers=headers, timeout=30.0
                )

            if response.status_code in (200, 201):
                return _json_text_content(response.json())

            return _json_error_content(
                _build_http_error_payload(response, "Error writing NES entity")
            )
        except httpx.TimeoutException:
            logger.warning("jawafdehi_submit_nes_change_timeout")
            return _mutation_timeout_content("Writing the NES entity")
        except httpx.HTTPError as e:
            logger.error("jawafdehi_submit_nes_change_http_error", error=str(e))
            return _error_text_content(f"Error writing NES entity: {str(e)}")
        except Exception as e:
            logger.exception(
                "jawafdehi_submit_nes_change_unexpected_error", error=str(e)
            )
            return _error_text_content(f"Unexpected error: {str(e)}")


class UploadMaterialFileTool(BaseTool):
    """Attach a file to a Material via the unified material upload endpoint.

    Post-unification the document/evidence store is Materials: this streams a
    local file to ``POST /api/materials/{source}/{ident}/file`` (multipart),
    which places it in object storage and appends a roled schema.org
    ``MediaObject`` to the material's ``associatedMedia`` (creating the material
    if it does not yet exist). Replaces the retired ``/api/sources`` upload.
    NGM-role gated.
    """

    @property
    def name(self) -> str:
        return "upload_material_file"

    @property
    def description(self) -> str:
        return (
            "Attach a file (from disk) to a Material at @id "
            "/material/{source}/{ident}, uploading it to storage as a roled "
            "MediaObject. Creates the material if it does not exist (then "
            "material_type is required). Local file uploads are only available "
            "through a local stdio MCP server."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": (
                        "Material source segment of the IRI "
                        "(e.g. 'nkp', 'court'), i.e. /material/{source}/{ident}."
                    ),
                },
                "ident": {
                    "type": "string",
                    "description": "Material ident segment of the IRI.",
                },
                "file_path": {
                    "type": "string",
                    "description": (
                        "Absolute path to the file on disk to upload. Only supported "
                        "by local stdio servers."
                    ),
                },
                "role": {
                    "type": "string",
                    "enum": [
                        "RAW",
                        "ALTERNATE",
                        "PERMALINK",
                        "MARKDOWN",
                        "SOURCE_PAGE",
                    ],
                    "description": "Link role for the uploaded file (default RAW).",
                    "default": "RAW",
                },
                "material_type": {
                    "type": "string",
                    "description": (
                        "Required only when CREATING a new material "
                        "(e.g. court_order). Ignored when the material exists."
                    ),
                },
                "source_url": {
                    "type": "string",
                    "format": "uri",
                    "description": "Optional original source URL recorded as provenance.",
                },
                "skip_convert": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Skip server-side OCR when authoritative extracted text "
                        "will be supplied separately."
                    ),
                },
            },
            "required": ["source", "ident", "file_path"],
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        if not is_local_stdio_transport():
            return _error_text_content(
                "Local file uploads are only supported by local stdio MCP servers. "
                "HTTP-hosted MCP servers cannot read server filesystem paths."
            )

        if not _has_upstream_auth():
            return _error_text_content(_NO_AUTH_MESSAGE)

        missing_keys = [
            k for k in ["source", "ident", "file_path"] if not arguments.get(k)
        ]
        if missing_keys:
            return _error_text_content(
                f"Missing required arguments: {', '.join(missing_keys)}"
            )

        file_path = Path(arguments["file_path"])
        try:
            file_size = file_path.stat().st_size
        except OSError as e:
            return _error_text_content(f"Could not read file '{file_path}': {e}")
        if file_size > MAX_MATERIAL_UPLOAD_BYTES:
            max_mb = MAX_MATERIAL_UPLOAD_BYTES // (1024 * 1024)
            return _error_text_content(
                f"File exceeds the {max_mb} MB material upload limit."
            )

        try:
            source, ident = encode_material_parts(
                arguments["source"], arguments["ident"]
            )
        except ValueError as exc:
            return _error_text_content(f"Error: {exc}")
        base_url = _get_jawafdehi_base_url()
        url = f"{base_url}/api/materials/{source}/{ident}/file"

        headers = _get_auth_headers()
        headers["Accept"] = "application/json"

        data: dict[str, str] = {}
        # input_schema "default" is metadata only (BaseTool doesn't inject it), so
        # apply the advertised RAW default here rather than sending no role.
        data["role"] = str(arguments.get("role") or "RAW")
        if arguments.get("material_type"):
            data["material_type"] = arguments["material_type"]
        if arguments.get("source_url"):
            data["source_url"] = str(arguments["source_url"])
        if "skip_convert" in arguments:
            data["skip_convert"] = "true" if arguments["skip_convert"] else "false"

        try:
            with file_path.open("rb") as file_handle:
                files = {"file": (file_path.name, file_handle)}
                async with httpx.AsyncClient(
                    timeout=120.0, **embedded_api_client_kwargs()
                ) as client:
                    response = await client.post(
                        url, headers=headers, data=data, files=files
                    )

            if response.status_code in (200, 201):
                return _json_text_content(response.json())

            return _json_error_content(
                _build_http_error_payload(response, "Error uploading material file")
            )
        except Exception as e:
            logger.exception("jawafdehi_upload_material_unexpected_error", error=str(e))
            return _error_text_content(f"Unexpected error uploading material: {str(e)}")
