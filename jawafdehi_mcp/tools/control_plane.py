"""Task-oriented tools for the unified Jawafdehi control plane."""

from __future__ import annotations

import json
from typing import Any, Iterable

import httpx
import structlog
from mcp.types import TextContent

from ..control_plane import request_control_plane
from ..path_safety import encode_entity_ref, encode_path_segment
from .base import BaseTool, ToolExecutionResult
from .control_plane_schemas import (
    browse_court_data_schema,
    browse_materials_schema,
    manage_case_update_proposals_schema,
    manage_casework_reviews_schema,
    manage_court_data_schema,
    manage_jobs_schema,
    manage_material_schema,
)

logger = structlog.get_logger()


def _text(payload: Any) -> list[TextContent]:
    return [
        TextContent(
            type="text",
            text=json.dumps(payload, indent=2, ensure_ascii=False),
        )
    ]


def _error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        _text({"success": False, "error": message}),
        is_error=True,
    )


def _required(arguments: dict[str, Any], *names: str) -> str | None:
    missing = [
        name
        for name in names
        if arguments.get(name) is None or arguments.get(name) == ""
    ]
    if not missing:
        return None
    return f"Missing required argument(s): {', '.join(missing)}."


def _query_params(
    arguments: dict[str, Any],
    names: Iterable[str],
) -> dict[str, Any]:
    return {
        name: arguments[name]
        for name in names
        if name in arguments and arguments[name] is not None
    }


def _path_segment(value: Any) -> str:
    return encode_path_segment(value)


class _ControlPlaneTool(BaseTool):
    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> list[TextContent] | ToolExecutionResult:
        try:
            payload = await request_control_plane(
                method,
                path,
                params=params,
                json_body=body,
                headers=headers,
            )
            content = _text(payload)
            if payload.get("success") is False:
                return ToolExecutionResult(content, is_error=True)
            return content
        except httpx.TimeoutException:
            logger.warning(
                "control_plane_timeout",
                tool_name=self.name,
                method=method,
                path=path,
            )
            if method.upper() in {"GET", "HEAD", "OPTIONS"}:
                return _error("Jawafdehi control-plane request timed out.")
            return _error(
                "Jawafdehi control-plane request timed out; the operation's "
                "outcome is unknown. Verify current state before retrying."
            )
        except httpx.HTTPError as exc:
            logger.error(
                "control_plane_http_error",
                tool_name=self.name,
                method=method,
                path=path,
                error=str(exc),
            )
            return _error(f"Jawafdehi control-plane HTTP error: {exc}")
        except ValueError as exc:
            return _error(str(exc))
        except Exception as exc:
            logger.exception(
                "control_plane_unexpected_error",
                tool_name=self.name,
                method=method,
                path=path,
            )
            return _error(f"Unexpected control-plane error: {exc}")


class SearchControlPlaneTool(_ControlPlaneTool):
    @property
    def name(self) -> str:
        return "search_control_plane"

    @property
    def description(self) -> str:
        return (
            "Search the unified public corpus across entities, materials, court "
            "cases, and published Jawafdehi cases."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "q": {"type": "string"},
                "type": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["entity", "material", "courtcase", "case"],
                    },
                    "uniqueItems": True,
                },
                "lang": {
                    "type": "string",
                    "enum": ["ne", "en", "both"],
                    "default": "both",
                },
                # Static (this schema must build without Django) but contract-tested
                # against search.service.ALL_SORTS, which rejects anything else 400.
                "sort": {
                    "type": "string",
                    "enum": ["relevance", "newest", "oldest", "title", "featured"],
                    "default": "relevance",
                },
                "entity_type": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "case_type": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "status": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                # बिगो (alleged embezzled amount, whole NPR) range bounds,
                # inclusive. CASE-ONLY: no entity/material/court-case document
                # carries an amount, so either bound also excludes every non-case
                # result — pair with type: ["case"].
                "bigo_min": {"type": "integer", "minimum": 0},
                "bigo_max": {"type": "integer", "minimum": 0},
                "page": {"type": "integer", "minimum": 1, "default": 1},
                "page_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                },
                "cursor": {"type": "string"},
            },
            "required": [],
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        params = _query_params(
            arguments,
            (
                "q",
                "type",
                "lang",
                "sort",
                "entity_type",
                "case_type",
                "tags",
                "status",
                "bigo_min",
                "bigo_max",
                "page",
                "page_size",
                "cursor",
            ),
        )
        return await self._call("GET", "/api/search/", params=params)


class GetNESEntityVersionsTool(_ControlPlaneTool):
    @property
    def name(self) -> str:
        return "get_nes_entity_versions"

    @property
    def description(self) -> str:
        return "Retrieve the version history of one NES entity."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Canonical entity IRI or prefix/slug reference.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 100,
                },
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["entity_id"],
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        if message := _required(arguments, "entity_id"):
            return _error(message)
        try:
            ref = encode_entity_ref(arguments["entity_id"])
        except ValueError as exc:
            return _error(str(exc))
        params = _query_params(arguments, ("limit", "offset"))
        return await self._call(
            "GET",
            f"/api/entities/{ref}/versions",
            params=params,
        )


class DeleteNESEntityTool(_ControlPlaneTool):
    @property
    def name(self) -> str:
        return "delete_nes_entity"

    @property
    def description(self) -> str:
        return (
            "Soft-delete an NES entity. The control plane enforces the caller's "
            "write role."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Canonical entity IRI or prefix/slug reference.",
                }
            },
            "required": ["entity_id"],
        }

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        if message := _required(arguments, "entity_id"):
            return _error(message)
        try:
            ref = encode_entity_ref(arguments["entity_id"])
        except ValueError as exc:
            return _error(str(exc))
        return await self._call("DELETE", f"/api/entities/{ref}")


class BrowseMaterialsTool(_ControlPlaneTool):
    ACTIONS = ("list", "get")

    @property
    def name(self) -> str:
        return "browse_materials"

    @property
    def description(self) -> str:
        return "List materials or retrieve one material by its canonical IRI."

    @property
    def input_schema(self) -> dict[str, Any]:
        return browse_materials_schema(self.ACTIONS)

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        action = arguments.get("action")
        if action == "get":
            if message := _required(arguments, "iri"):
                return _error(message)
            return await self._call(
                "GET",
                "/api/materials/",
                params={"iri": arguments["iri"]},
            )
        if action == "list":
            params = _query_params(arguments, ("source", "material_type", "cursor"))
            return await self._call("GET", "/api/materials/", params=params)
        return _error("action must be 'list' or 'get'.")


class ManageMaterialTool(_ControlPlaneTool):
    ACTIONS = ("upsert", "patch", "delete")

    @property
    def name(self) -> str:
        return "manage_material"

    @property
    def description(self) -> str:
        return (
            "Upsert, JSON-Patch, or soft-delete a material. Existing control-plane "
            "permissions and optional ETag preconditions remain authoritative."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return manage_material_schema(self.ACTIONS)

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        action = arguments.get("action")
        if action == "upsert":
            if message := _required(arguments, "material"):
                return _error(message)
            body = {"material": arguments["material"]}
            for name in ("material_type", "visibility_policy"):
                if name in arguments:
                    body[name] = arguments[name]
            return await self._call("POST", "/api/materials/", body=body)

        if action == "patch":
            if message := _required(arguments, "iri"):
                return _error(message)
            if "patch_ops" not in arguments and "visibility_policy" not in arguments:
                return _error("Material patch requires patch_ops or visibility_policy.")
            body = {
                name: arguments[name]
                for name in ("patch_ops", "visibility_policy")
                if name in arguments
            }
            headers = (
                {"If-Match": str(arguments["if_match"])}
                if arguments.get("if_match")
                else None
            )
            return await self._call(
                "PATCH",
                "/api/materials/",
                params={"iri": arguments["iri"]},
                body=body,
                headers=headers,
            )

        if action == "delete":
            if message := _required(arguments, "iri"):
                return _error(message)
            return await self._call(
                "DELETE",
                "/api/materials/",
                params={"iri": arguments["iri"]},
            )

        return _error("Unknown material action.")


class BrowseCourtDataTool(_ControlPlaneTool):
    ACTIONS = (
        "list_courts",
        "get_court",
        "list_cases",
        "get_case",
        "list_hearings",
        "list_entities",
        "list_documents",
        "search_entities",
        "list_firms",
        "get_firm",
    )

    @property
    def name(self) -> str:
        return "browse_court_data"

    @property
    def description(self) -> str:
        return (
            "Browse courts, court cases and their subresources, resolved case "
            "entities, and blacklisted firms."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return browse_court_data_schema(self.ACTIONS)

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        action = arguments.get("action")
        if action == "list_courts":
            return await self._call("GET", "/api/courts/")
        if action == "get_court":
            if message := _required(arguments, "court"):
                return _error(message)
            return await self._call(
                "GET", f"/api/courts/{_path_segment(arguments['court'])}/"
            )
        if action == "list_cases":
            params = _query_params(
                arguments,
                ("court", "type", "status", "date_from", "date_to", "page"),
            )
            return await self._call("GET", "/api/courtcases/", params=params)
        if action in {
            "get_case",
            "list_hearings",
            "list_entities",
            "list_documents",
        }:
            if message := _required(arguments, "court", "case_number"):
                return _error(message)
            base = (
                f"/api/courtcases/{_path_segment(arguments['court'])}/"
                f"{_path_segment(arguments['case_number'])}"
            )
            suffix = {
                "get_case": "/",
                "list_hearings": "/hearings/",
                "list_entities": "/entities/",
                "list_documents": "/documents/",
            }[action]
            params = (
                _query_params(arguments, ("page",))
                if action in {"list_hearings", "list_entities"}
                else None
            )
            return await self._call("GET", f"{base}{suffix}", params=params)
        if action == "search_entities":
            params = _query_params(arguments, ("nes_id", "name", "page"))
            return await self._call("GET", "/api/courtcase-entities/", params=params)
        if action == "list_firms":
            params = _query_params(arguments, ("nes_id", "name", "page"))
            return await self._call("GET", "/api/firms/", params=params)
        if action == "get_firm":
            if message := _required(arguments, "firm_id"):
                return _error(message)
            return await self._call("GET", f"/api/firms/{int(arguments['firm_id'])}/")
        return _error("Unknown court-data browse action.")


class ManageCourtDataTool(_ControlPlaneTool):
    ACTIONS = (
        "create_court",
        "update_court",
        "create_case",
        "update_case",
        "delete_case",
        "ingest_cases",
        "resolve_entities",
        "register_documents",
        "ingest_firms",
        "create_firm",
        "update_firm",
    )

    @property
    def name(self) -> str:
        return "manage_court_data"

    @property
    def description(self) -> str:
        return (
            "Create or update courts and court cases, soft-delete a court case, "
            "or invoke a bounded court-data ingestion operation."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return manage_court_data_schema(self.ACTIONS)

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        action = arguments.get("action")
        if action == "create_court":
            if message := _required(arguments, "body"):
                return _error(message)
            return await self._call("POST", "/api/courts/", body=arguments["body"])
        if action == "update_court":
            if message := _required(arguments, "court", "body"):
                return _error(message)
            return await self._call(
                "PATCH",
                f"/api/courts/{_path_segment(arguments['court'])}/",
                body=arguments["body"],
            )
        if action == "create_case":
            if message := _required(arguments, "body"):
                return _error(message)
            return await self._call("POST", "/api/courtcases/", body=arguments["body"])
        if action in {"update_case", "delete_case"}:
            if message := _required(arguments, "court", "case_number"):
                return _error(message)
            path = (
                f"/api/courtcases/{_path_segment(arguments['court'])}/"
                f"{_path_segment(arguments['case_number'])}/"
            )
            if action == "delete_case":
                return await self._call("DELETE", path)
            if message := _required(arguments, "body"):
                return _error(message)
            return await self._call("PATCH", path, body=arguments["body"])

        if action == "create_firm":
            if message := _required(arguments, "body"):
                return _error(message)
            return await self._call("POST", "/api/firms/", body=arguments["body"])
        if action == "update_firm":
            if message := _required(arguments, "firm_id", "body"):
                return _error(message)
            return await self._call(
                "PATCH",
                f"/api/firms/{int(arguments['firm_id'])}/",
                body=arguments["body"],
            )

        ingestion_paths = {
            "ingest_cases": "/api/ingestion/cases/",
            "resolve_entities": "/api/ingestion/entities/resolve/",
            "register_documents": "/api/ingestion/documents/",
            "ingest_firms": "/api/ingestion/firms/",
        }
        if action in ingestion_paths:
            if message := _required(arguments, "items"):
                return _error(message)
            return await self._call(
                "POST",
                ingestion_paths[action],
                body={"items": arguments["items"]},
            )
        return _error("Unknown court-data management action.")


class ManageCaseUpdateProposalsTool(_ControlPlaneTool):
    ACTIONS = ("list", "get", "create", "edit", "approve", "reject")

    @property
    def name(self) -> str:
        return "manage_case_update_proposals"

    @property
    def description(self) -> str:
        return (
            "List, retrieve, create, edit, approve, or reject human-reviewed "
            "case-update proposals."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return manage_case_update_proposals_schema(self.ACTIONS)

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        action = arguments.get("action")
        collection = "/api/case-update-proposals/"
        if action == "list":
            params = _query_params(
                arguments, ("status", "source_kind", "case_slug", "page")
            )
            return await self._call("GET", collection, params=params)
        if action == "create":
            if message := _required(arguments, "body"):
                return _error(message)
            return await self._call("POST", collection, body=arguments["body"])
        if action in {"get", "edit", "approve", "reject"}:
            if message := _required(arguments, "proposal_id"):
                return _error(message)
            base = f"{collection}{int(arguments['proposal_id'])}/"
            if action == "get":
                return await self._call("GET", base)
            if action == "edit":
                if message := _required(arguments, "body"):
                    return _error(message)
                return await self._call(
                    "PATCH", f"{base}intent/", body=arguments["body"]
                )
            return await self._call(
                "POST",
                f"{base}{action}/",
                body=arguments.get("body") or {},
            )
        return _error("Unknown case-update proposal action.")


class ManageCaseworkReviewsTool(_ControlPlaneTool):
    ACTIONS = (
        "list",
        "list_grouped",
        "get",
        "submit",
        "list_rules",
        "get_rule",
        "get_config",
        "update_config",
        "regrade_all",
    )

    @property
    def name(self) -> str:
        return "manage_casework_reviews"

    @property
    def description(self) -> str:
        return (
            "Inspect and submit casework reviews, read rules, manage review "
            "configuration, or request a full regrade."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return manage_casework_reviews_schema(self.ACTIONS)

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        action = arguments.get("action")
        if action in {"list", "list_grouped"}:
            path = (
                "/api/casework/reviews/grouped/"
                if action == "list_grouped"
                else "/api/casework/reviews/"
            )
            params = _query_params(arguments, ("slug", "page"))
            return await self._call("GET", path, params=params)
        if action == "get":
            if message := _required(arguments, "review_id"):
                return _error(message)
            return await self._call(
                "GET",
                f"/api/casework/reviews/{int(arguments['review_id'])}/",
            )
        if action == "submit":
            if message := _required(arguments, "body"):
                return _error(message)
            return await self._call(
                "POST",
                "/api/casework/reviews/submit/",
                body=arguments["body"],
            )
        if action == "list_rules":
            return await self._call("GET", "/api/casework/rules/")
        if action == "get_rule":
            if message := _required(arguments, "rule_id"):
                return _error(message)
            return await self._call(
                "GET", f"/api/casework/rules/{int(arguments['rule_id'])}/"
            )
        if action == "get_config":
            return await self._call("GET", "/api/casework/config/")
        if action == "update_config":
            if message := _required(arguments, "body"):
                return _error(message)
            return await self._call(
                "PUT", "/api/casework/config/", body=arguments["body"]
            )
        if action == "regrade_all":
            return await self._call(
                "POST", "/api/casework/reviews/regrade-all/", body={}
            )
        return _error("Unknown casework review action.")


class ManageJobsTool(_ControlPlaneTool):
    ACTIONS = ("list", "enqueue", "claim", "stage", "result")

    @property
    def name(self) -> str:
        return "manage_jobs"

    @property
    def description(self) -> str:
        return (
            "Observe, enqueue, claim, heartbeat, or finalize jobs through the "
            "central control-plane queue."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return manage_jobs_schema(self.ACTIONS)

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        action = arguments.get("action")
        if action == "list":
            params = _query_params(arguments, ("kind", "status", "limit"))
            return await self._call("GET", "/api/jobs/", params=params)
        if action == "enqueue":
            if message := _required(arguments, "body"):
                return _error(message)
            return await self._call("POST", "/api/jobs/", body=arguments["body"])
        if action == "claim":
            if message := _required(arguments, "body"):
                return _error(message)
            return await self._call("POST", "/api/jobs/claim/", body=arguments["body"])
        if action in {"stage", "result"}:
            if message := _required(arguments, "job_id", "body"):
                return _error(message)
            return await self._call(
                "POST",
                f"/api/jobs/{int(arguments['job_id'])}/{action}/",
                body=arguments["body"],
            )
        return _error("Unknown jobs action.")
