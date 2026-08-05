import json
import os
import urllib.parse
from typing import Any

import httpx
import structlog
from mcp.types import TextContent

from ..api_transport import embedded_api_client_kwargs
from ..request_context import get_forwarded_headers, get_local_service_token
from .base import BaseTool, ToolExecutionResult, error_text

logger = structlog.get_logger()


def _get_nes_base_url() -> str:
    """Base URL for entity reads.

    Post-unification NES entities are served by the ONE Jawafdehi host under a
    bare ``/api/entities`` (the standalone ``nes.jawafdehi.org`` service + its
    ``/api/nes`` prefix were retired in the 2026-07 hard cut, with no override:
    a legacy NES base URL would silently read the frozen pre-cutover backend).
    """
    return os.getenv("JAWAFDEHI_API_BASE_URL", "https://api.jawafdehi.org").rstrip("/")


def _get_nes_headers() -> dict[str, str]:
    """Auth headers for entity reads.

    Forward the caller's OIDC bearer when present (HTTP transport); otherwise
    fall back to the service token as ``Bearer`` (stdio/dev). Mirrors the write
    tools so token-only flows keep working once the unified API requires auth.
    """
    headers = get_forwarded_headers()
    if "Authorization" not in headers:
        token = get_local_service_token()
        if token:
            headers = {"Authorization": f"Bearer {token}"}
    return headers


def _build_text_response(payload: Any) -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text=json.dumps(payload, indent=2, ensure_ascii=False),
        )
    ]


def _extract_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or response.reason_phrase

    if isinstance(payload, dict):
        return json.dumps(payload, indent=2, ensure_ascii=False)

    return str(payload)


class SearchNESEntitiesTool(BaseTool):
    """Tool for searching NES entities."""

    @property
    def name(self) -> str:
        return "search_nes_entities"

    @property
    def description(self) -> str:
        return (
            "Search or browse Nepal Entity Service (NES) entities by text, "
            "primary type, canonical prefix, and tags."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entity_type": {
                    "type": "string",
                    "description": "Filter by primary entity type (e.g., person, organization, location).",
                },
                "entity_prefix": {
                    "type": "string",
                    "description": (
                        "Filter by canonical entity prefix (e.g. person or "
                        "organization/political_party)."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "Text query to search in entity names (e.g., 'poudel').",
                },
                "tags": {
                    "type": "string",
                    "description": "Comma-separated tags to filter by (uses AND logic, e.g., 'politician,senior-leader').",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 100,
                    "description": "Maximum number of entities to return.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Number of results to skip (default is 0).",
                    "default": 0,
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        query_params = {"limit": str(arguments.get("limit", 100))}

        if arguments.get("query"):
            query_params["query"] = arguments["query"]
        if arguments.get("entity_type"):
            query_params["entity_type"] = arguments["entity_type"]
        if arguments.get("entity_prefix"):
            query_params["entity_prefix"] = arguments["entity_prefix"]
        if arguments.get("tags"):
            query_params["tags"] = arguments["tags"]
        if "offset" in arguments:
            query_params["offset"] = str(arguments["offset"])

        query_string = urllib.parse.urlencode(query_params)
        url = f"{_get_nes_base_url()}/api/entities?{query_string}"

        try:
            async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
                response = await client.get(
                    url, headers=_get_nes_headers(), timeout=30.0
                )
                response.raise_for_status()
                data = response.json()

                return [
                    TextContent(
                        type="text", text=json.dumps(data, indent=2, ensure_ascii=False)
                    )
                ]
        except httpx.HTTPError as e:
            logger.error("nes_search_http_error", error=str(e))
            return error_text(
                f"Error accessing NES API: {str(e)}\n\n"
                "Consider narrowing your search or checking parameters."
            )
        except Exception as e:
            logger.exception("nes_search_unexpected_error", error=str(e))
            return error_text(f"Unexpected error: {str(e)}")


class GetNESEntitiesTool(BaseTool):
    """Tool for retrieving detailed info on one or more NES entities by ID."""

    @property
    def name(self) -> str:
        return "get_nes_entities"

    @property
    def description(self) -> str:
        return "Retrieve the complete profiles of one or more NES entities by their unique IDs."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entity_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 100,
                    "description": (
                        "Canonical entity IRIs to retrieve, for example "
                        "['https://jawafdehi.org/entity/person/"
                        "ram-chandra-poudel']."
                    ),
                },
            },
            "required": ["entity_ids"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        entity_ids = arguments.get("entity_ids")
        if not entity_ids or not isinstance(entity_ids, list):
            return error_text(
                "Error: entity_ids must be a non-empty list of strings."
            )

        if len(entity_ids) > 100:
            return error_text(
                "Error: at most 100 entity_ids may be requested per call."
            )
        if any(
            not isinstance(entity_id, str) or not entity_id.strip()
            for entity_id in entity_ids
        ):
            return error_text("Error: every entity_id must be a non-empty string.")

        all_entities = []
        errors = []

        chunk_size = 25
        base_url = _get_nes_base_url()

        async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
            for i in range(0, len(entity_ids), chunk_size):
                chunk = entity_ids[i : i + chunk_size]
                ids_str = ",".join(chunk)
                url = f"{base_url}/api/entities?ids={urllib.parse.quote(ids_str)}"

                try:
                    response = await client.get(
                        url, headers=_get_nes_headers(), timeout=30.0
                    )
                    response.raise_for_status()
                    data = response.json()

                    if "entities" in data:
                        all_entities.extend(data["entities"])
                    else:
                        errors.append(
                            f"Unexpected response format for chunk {i // chunk_size + 1}"
                        )

                except httpx.HTTPError as e:
                    logger.error(
                        "nes_get_entities_http_error",
                        chunk=i // chunk_size + 1,
                        error=str(e),
                    )
                    errors.append(
                        f"HTTP Error for chunk {i // chunk_size + 1}: {str(e)}"
                    )
                except Exception as e:
                    logger.exception(
                        "nes_get_entities_unexpected_error",
                        chunk=i // chunk_size + 1,
                        error=str(e),
                    )
                    errors.append(
                        f"Unexpected error for chunk {i // chunk_size + 1}: {str(e)}"
                    )

        result = {
            "entities": all_entities,
            "total_requested": len(entity_ids),
            "total_found": len(all_entities),
        }

        if errors:
            result["errors"] = errors

        content = [
            TextContent(
                type="text", text=json.dumps(result, indent=2, ensure_ascii=False)
            )
        ]
        if errors:
            return ToolExecutionResult(content, is_error=True)
        return content


class GetNESTagsTool(BaseTool):
    """Tool for fetching the complete list of unique entity tags."""

    @property
    def name(self) -> str:
        return "get_nes_tags"

    @property
    def description(self) -> str:
        return "Fetch the complete list of all unique entity tag values present in the NES database."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        url = f"{_get_nes_base_url()}/api/entities/tags"

        try:
            async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
                response = await client.get(
                    url, headers=_get_nes_headers(), timeout=30.0
                )
                response.raise_for_status()
                data = response.json()

                return [
                    TextContent(
                        type="text", text=json.dumps(data, indent=2, ensure_ascii=False)
                    )
                ]
        except httpx.HTTPError as e:
            logger.error("nes_tags_http_error", error=str(e))
            return error_text(f"Error accessing NES Tags API: {str(e)}")
        except Exception as e:
            logger.exception("nes_tags_unexpected_error", error=str(e))
            return error_text(f"Unexpected error: {str(e)}")


class GetNESEntityPrefixesTool(BaseTool):
    """Tool for fetching available NES entity prefixes."""

    @property
    def name(self) -> str:
        return "get_nes_entity_prefixes"

    @property
    def description(self) -> str:
        return "Fetch the available NES entity prefixes and related metadata."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        url = f"{_get_nes_base_url()}/api/entity_prefixes"

        try:
            async with httpx.AsyncClient(**embedded_api_client_kwargs()) as client:
                response = await client.get(
                    url, headers=_get_nes_headers(), timeout=30.0
                )

            if response.status_code == 200:
                return _build_text_response(response.json())

            error_message = _extract_error_message(response)
            return error_text(
                "Error fetching NES entity prefixes: "
                f"HTTP {response.status_code}\n\n{error_message}"
            )
        except httpx.TimeoutException:
            logger.warning("nes_prefixes_timeout")
            return error_text(
                "Error fetching NES entity prefixes: request timed out."
            )
        except httpx.HTTPError as exc:
            logger.error("nes_prefixes_http_error", error=str(exc))
            return error_text(f"Error fetching NES entity prefixes: {str(exc)}")
        except Exception as exc:
            logger.exception("nes_prefixes_unexpected_error", error=str(exc))
            return error_text(f"Unexpected error: {str(exc)}")
