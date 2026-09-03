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


#: The facet names ``facet_q`` accepts on the left of its colon — i.e. the keys of
#: ``search.service.FACET_FIELDS``, restated here BY CONVENTION: this schema is
#: kept buildable without the Django app, as the ``sort`` enum is. Note it is only
#: a convention, not a constraint — nothing enforces it, ``import search.service``
#: succeeds with no settings configured, and ``tools/ngm_judicial.py`` already
#: imports ``courts`` at module level — so if this list grows a third copy, prefer
#: importing the registry to restating it. Pinned to that registry by
#: ``test_search_facet_q_facets_track_facet_fields``.
_FACET_Q_FACETS: tuple[str, ...] = (
    "entity_type",
    "case_type",
    "tags",
    "status",
    "court",
    "court_type",
    "district",
    "province",
    "material_type",
    "material_source",
)

#: Mirror of ``search.service.MAX_FACET_Q_TEXT``, pinned by
#: ``test_search_facet_q_length_matches_the_endpoints_limit`` so the advertised
#: limit cannot promise more than the endpoint accepts.
_MAX_FACET_Q_TEXT = 200


class SearchControlPlaneTool(_ControlPlaneTool):
    #: The query params forwarded to ``/api/search/``. A single list, used by both
    #: ``input_schema`` and ``execute``, so the advertised surface and the
    #: forwarded surface cannot drift apart — a param declared but not forwarded
    #: fails silently, which is how the बिगो bounds first shipped half-wired.
    #: ``test_unified_search_schema_tracks_the_search_endpoint`` pins this to
    #: ``SearchQuerySerializer``.
    PARAMS: tuple[str, ...] = (
        "q",
        "type",
        "lang",
        "sort",
        "entity_type",
        "case_type",
        "tags",
        "status",
        "court",
        "court_type",
        "district",
        "province",
        "material_type",
        "material_source",
        "bigo_min",
        "bigo_max",
        "date_from",
        "date_to",
        "facet_q",
        "page",
        "page_size",
        "cursor",
    )

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
                # ONE-court facet: the deciding court's identifier, e.g.
                # "kathmandudc" / "patanhc" / "supreme". COURTCASE-ONLY: only NGM
                # court cases carry a court, so any value excludes every other
                # result type — pair with type: ["courtcase"]. Repeatable, so an
                # arbitrary set of courts is selectable; court_type and district
                # AND together and cannot express one.
                #
                # Left as a plain string (not a 97-value enum) for the same reason
                # district/province are: too large to restate here without it
                # drifting. The API validates it against a closed list and 400s
                # on anything else; GET /api/courts/ enumerates the 97.
                "court": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
                # Court tier facet (courtcase-only, same pairing caveat). Static
                # (this schema must build without Django) but contract-tested
                # against search.service.ALL_COURT_TYPES — the tuple the
                # endpoint's ChoiceField and the OpenAPI enum are both built
                # from — by test_search_court_type_enum_tracks_all_court_types.
                "court_type": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["district", "high", "supreme", "special"],
                    },
                    "uniqueItems": True,
                },
                # Court geography facets (courtcase-only, same pairing caveat).
                # Values are the canonical English names the response's
                # facets.district / facets.province buckets return.
                #
                # "district" is a DISTRICT COURT's own district and matches
                # nothing else — high courts are provincial and carry no
                # district, supreme/special carry none. "province" covers all 95
                # sub-national courts; "NATIONAL" selects supreme + special.
                "district": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "A DISTRICT COURT's own district, as the canonical "
                        "Title-Case English name returned in facets.district — "
                        "e.g. 'Kathmandu', not 'kathmandu'. Values are "
                        "CASE-SENSITIVE and a wrong case returns an empty result "
                        "set rather than an error, so take values from the "
                        "response's facets.district buckets. Matches "
                        "district-court cases ONLY: a high court is a provincial "
                        "court and carries no district (use province), and "
                        "supreme/special carry none either. Courtcase-only."
                    ),
                },
                "province": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "The court's province, as the canonical Title-Case "
                        "English name returned in facets.province — one of "
                        "Koshi, Madhesh, Bagmati, Gandaki, Lumbini, Karnali, "
                        "Sudurpashchim — or the UPPER-CASE sentinel 'NATIONAL', "
                        "which selects supreme + special-court cases. Values are "
                        "CASE-SENSITIVE and a wrong case returns an empty result "
                        "set rather than an error. Set for all 95 sub-national "
                        "courts, a high court resolving to the province it "
                        "serves (its additional benches included). "
                        "Courtcase-only."
                    ),
                },
                # The two material provenance facets (material-only, and they
                # AND together). Separate axes: one office publishes several
                # types, one type comes from several offices.
                "material_type": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "charge_sheet",
                            "court_case",
                            "court_order",
                            "document",
                            "legal_corpus",
                            "manuscript",
                            "news",
                            "official_report",
                            "precedent",
                            "press_release",
                            "procurement_notice",
                            "social_media",
                        ],
                    },
                    "uniqueItems": True,
                    "description": (
                        "What KIND of material a document is. A closed "
                        "vocabulary, so an unlisted value is a 400 rather than "
                        "an empty page. Material-only: any value also excludes "
                        "every entity, court-case and case result, so pair it "
                        "with type: [\"material\"]."
                    ),
                },
                "material_source": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "WHICH office or feed published a material — the ingest "
                        "source token, lower_snake_case ('ag', "
                        "'ciaa_press_release', 'bolpatra'). Open-ended, so take "
                        "values from the response's facets.material_source "
                        "buckets or list them with "
                        "facet_q: [\"material_source:<text>\"]; an unknown token "
                        "returns an empty result set rather than an error. A "
                        "SEPARATE axis from material_type, not a restatement: "
                        "the CIAA publishes press releases AND annual reports, "
                        "while press releases come from ciaa_press_release, cib "
                        "and dmli. Material-only."
                    ),
                },
                # बिगो (alleged embezzled amount, whole NPR) range bounds,
                # inclusive. CASE-ONLY: no entity/material/court-case document
                # carries an amount, so either bound also excludes every non-case
                # result — pair with type: ["case"].
                # Bounds mirror the endpoint's own validation, so a schema-valid
                # call cannot come back as a 400 from the API's clamp.
                "bigo_min": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
                "bigo_max": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
                # Gregorian date-range bounds (inclusive) over the shared record
                # date. Entities carry no date, so either bound excludes every
                # entity result.
                "date_from": {"type": "string", "format": "date"},
                "date_to": {"type": "string", "format": "date"},
                # Facet-value search: "<facet>:<text>" recomputes only that
                # facet's bucket list to buckets containing <text> (case-
                # insensitive, over the full aggregation) without affecting
                # results, count, or other facets. Repeatable, once per facet.
                "facet_q": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Facet-value search, one entry per facet, each of the form "
                        "'<facet>:<text>' — e.g. 'district:kath'. The FORM IS "
                        "MANDATORY: an entry with no colon is a 400. <facet> must "
                        "be one of "
                        + ", ".join(sorted(_FACET_Q_FACETS))
                        + f"; <text> is matched literally (regex-escaped "
                        f"server-side), case-insensitively, and is limited to "
                        f"{_MAX_FACET_Q_TEXT} characters. Recomputes ONLY that "
                        "facet's bucket list to the buckets whose key contains "
                        "<text>, matched over the full aggregation; results, "
                        "count and every other facet are unaffected. This is a "
                        "typeahead for picking a filter value, NOT a way to "
                        "filter results."
                    ),
                },
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
        params = _query_params(arguments, self.PARAMS)
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
