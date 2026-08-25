"""Contract tests for task-oriented control-plane MCP tools."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from jsonschema import Draft202012Validator

from jawafdehi_mcp.server import TOOL_MAP
from jawafdehi_mcp.tools.control_plane import (
    BrowseCourtDataTool,
    BrowseMaterialsTool,
    DeleteNESEntityTool,
    GetNESEntityVersionsTool,
    ManageCaseUpdateProposalsTool,
    ManageCaseworkReviewsTool,
    ManageCourtDataTool,
    ManageJobsTool,
    ManageMaterialTool,
    SearchControlPlaneTool,
)
from jawafdehi_mcp.tools.control_plane_schemas import (
    COURT_CASE_CREATE_BODY_SCHEMA,
    COURT_CREATE_BODY_SCHEMA,
    FIRM_CREATE_BODY_SCHEMA,
    FIRM_INGEST_ITEM_SCHEMA,
    JOB_CLAIM_BODY_SCHEMA,
    JOB_ENQUEUE_BODY_SCHEMA,
    JOB_RESULT_BODY_SCHEMA,
    JOB_STAGE_BODY_SCHEMA,
    PROPOSAL_CREATE_BODY_SCHEMA,
    PROPOSAL_DECISION_BODY_SCHEMA,
    PROPOSAL_EDIT_BODY_SCHEMA,
    REVIEW_CONFIG_BODY_SCHEMA,
    REVIEW_SUBMIT_BODY_SCHEMA,
)
from case_proposals.serializers import (
    CaseUpdateProposalSerializer,
    ProposalDecisionSerializer,
    ProposalIntentEditSerializer,
)
from courts.serializers import (
    BlacklistedFirmSerializer,
    BlacklistedFirmWriteSerializer,
    CourtCaseWriteSerializer,
    CourtSerializer,
)
from jobs.serializers import (
    JobClaimSerializer,
    JobEnqueueSerializer,
    JobResultSerializer,
    JobStageSerializer,
)
from review.serializers import ReviewConfigSerializer, SubmitSerializer
from search.service import ALL_SORTS


async def _call(tool, arguments):
    request = AsyncMock(
        return_value={"success": True, "status_code": 200, "data": {"ok": True}}
    )
    with patch(
        "jawafdehi_mcp.tools.control_plane.request_control_plane",
        new=request,
    ):
        result = await tool.execute(arguments)
    return request, json.loads(result[0].text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("GET", "request timed out."),
        ("POST", "outcome is unknown"),
    ],
)
async def test_timeout_distinguishes_reads_from_mutations(method, expected):
    tool = SearchControlPlaneTool()
    with patch(
        "jawafdehi_mcp.tools.control_plane.request_control_plane",
        new=AsyncMock(side_effect=httpx.ReadTimeout("deadline")),
    ):
        result = await tool._call(method, "/api/search/")

    assert result.is_error is True
    assert expected in json.loads(result[0].text)["error"]


def test_parity_tools_are_registered():
    assert {
        "search_control_plane",
        "get_nes_entity_versions",
        "delete_nes_entity",
        "browse_materials",
        "manage_material",
        "browse_court_data",
        "manage_court_data",
        "manage_case_update_proposals",
        "manage_casework_reviews",
        "manage_jobs",
    }.issubset(TOOL_MAP)


def _writable_field_names(serializer_class):
    return {
        name for name, field in serializer_class().fields.items() if not field.read_only
    }


@pytest.mark.parametrize(
    ("schema", "serializer_class"),
    [
        (COURT_CREATE_BODY_SCHEMA, CourtSerializer),
        (COURT_CASE_CREATE_BODY_SCHEMA, CourtCaseWriteSerializer),
        (FIRM_CREATE_BODY_SCHEMA, BlacklistedFirmSerializer),
        (FIRM_INGEST_ITEM_SCHEMA, BlacklistedFirmWriteSerializer),
        (PROPOSAL_CREATE_BODY_SCHEMA, CaseUpdateProposalSerializer),
        (PROPOSAL_EDIT_BODY_SCHEMA, ProposalIntentEditSerializer),
        (PROPOSAL_DECISION_BODY_SCHEMA, ProposalDecisionSerializer),
        (REVIEW_SUBMIT_BODY_SCHEMA, SubmitSerializer),
        (REVIEW_CONFIG_BODY_SCHEMA, ReviewConfigSerializer),
        (JOB_ENQUEUE_BODY_SCHEMA, JobEnqueueSerializer),
        (JOB_CLAIM_BODY_SCHEMA, JobClaimSerializer),
        (JOB_STAGE_BODY_SCHEMA, JobStageSerializer),
        (JOB_RESULT_BODY_SCHEMA, JobResultSerializer),
    ],
)
def test_control_plane_body_schemas_track_drf_serializers(schema, serializer_class):
    assert set(schema["properties"]) == _writable_field_names(serializer_class)


def test_search_sort_enum_tracks_all_sorts():
    """The tool schema is static so it builds without Django, so nothing stops it
    drifting from the ChoiceField that ``/api/search/`` actually validates against —
    a mode missing here is unreachable, and one that shouldn't be here 400s."""
    schema = SearchControlPlaneTool().input_schema
    assert schema["properties"]["sort"]["enum"] == list(ALL_SORTS)


def test_unified_search_schema_tracks_the_search_endpoint():
    """The search tool is a passthrough, so its three surfaces must agree.

    A param the endpoint accepts but the tool omits is invisible to MCP clients; a
    param the tool advertises but does not forward is worse — it is accepted and
    silently ignored. Both are drift this pins down, the same way the body schemas
    above are pinned to their serializers.
    """
    from search.views import SearchQuerySerializer

    tool = SearchControlPlaneTool()
    endpoint_params = set(SearchQuerySerializer().fields)
    assert set(tool.input_schema["properties"]) == endpoint_params
    assert set(tool.PARAMS) == endpoint_params


def test_unified_search_numeric_bounds_match_the_endpoints_validation():
    """A schema-valid call must not be rejected by the API's own clamp.

    The बिगो bounds are clamped to the signed-64-bit domain of the index's ``long``
    mapping; advertising a wider range would let an MCP client build a request the
    schema calls valid and the endpoint answers with a 400.
    """
    from search.views import SearchQuerySerializer

    fields = SearchQuerySerializer().fields
    properties = SearchControlPlaneTool().input_schema["properties"]
    for name in ("bigo_min", "bigo_max"):
        assert properties[name]["minimum"] == fields[name].min_value, name
        assert properties[name]["maximum"] == fields[name].max_value, name


@pytest.mark.parametrize(
    "tool",
    [
        BrowseMaterialsTool(),
        ManageMaterialTool(),
        BrowseCourtDataTool(),
        ManageCourtDataTool(),
        ManageCaseUpdateProposalsTool(),
        ManageCaseworkReviewsTool(),
        ManageJobsTool(),
    ],
    ids=lambda tool: tool.name,
)
def test_every_aggregate_action_has_a_schema_contract(tool):
    schema = tool.input_schema
    actions = set(tool.ACTIONS)
    conditional_actions = {
        rule["if"]["properties"]["action"]["const"] for rule in schema["allOf"]
    }

    assert set(schema["properties"]["action"]["enum"]) == actions
    assert conditional_actions == actions


ACTION_ROUTE_CASES = [
    pytest.param(
        BrowseMaterialsTool(),
        {"action": "list", "source": "nkp", "cursor": "next"},
        "GET",
        "/api/materials/",
        {"source": "nkp", "cursor": "next"},
        None,
        id="materials-list",
    ),
    pytest.param(
        BrowseMaterialsTool(),
        {"action": "get", "iri": "https://jawafdehi.org/material/nkp/1"},
        "GET",
        "/api/materials/",
        {"iri": "https://jawafdehi.org/material/nkp/1"},
        None,
        id="materials-get",
    ),
    pytest.param(
        ManageMaterialTool(),
        {
            "action": "upsert",
            "material": {
                "@id": "https://jawafdehi.org/material/nkp/1",
                "@type": "Legislation",
            },
        },
        "POST",
        "/api/materials/",
        None,
        {
            "material": {
                "@id": "https://jawafdehi.org/material/nkp/1",
                "@type": "Legislation",
            }
        },
        id="materials-upsert",
    ),
    pytest.param(
        ManageMaterialTool(),
        {
            "action": "patch",
            "iri": "https://jawafdehi.org/material/nkp/1",
            "visibility_policy": "PRIVATE",
        },
        "PATCH",
        "/api/materials/",
        {"iri": "https://jawafdehi.org/material/nkp/1"},
        {"visibility_policy": "PRIVATE"},
        id="materials-patch",
    ),
    pytest.param(
        ManageMaterialTool(),
        {
            "action": "delete",
            "iri": "https://jawafdehi.org/material/nkp/1",
        },
        "DELETE",
        "/api/materials/",
        {"iri": "https://jawafdehi.org/material/nkp/1"},
        None,
        id="materials-delete",
    ),
    pytest.param(
        BrowseCourtDataTool(),
        {"action": "list_courts"},
        "GET",
        "/api/courts/",
        None,
        None,
        id="courts-list",
    ),
    pytest.param(
        BrowseCourtDataTool(),
        {"action": "get_court", "court": "special"},
        "GET",
        "/api/courts/special/",
        None,
        None,
        id="courts-get",
    ),
    pytest.param(
        BrowseCourtDataTool(),
        {"action": "list_cases", "court": "special", "page": 3},
        "GET",
        "/api/courtcases/",
        {"court": "special", "page": 3},
        None,
        id="court-cases-list",
    ),
    pytest.param(
        BrowseCourtDataTool(),
        {"action": "get_case", "court": "special", "case_number": "082-CR-1"},
        "GET",
        "/api/courtcases/special/082-CR-1/",
        None,
        None,
        id="court-cases-get",
    ),
    pytest.param(
        BrowseCourtDataTool(),
        {
            "action": "list_hearings",
            "court": "special",
            "case_number": "082-CR-1",
            "page": 2,
        },
        "GET",
        "/api/courtcases/special/082-CR-1/hearings/",
        {"page": 2},
        None,
        id="court-hearings-list",
    ),
    pytest.param(
        BrowseCourtDataTool(),
        {
            "action": "list_entities",
            "court": "special",
            "case_number": "082-CR-1",
            "page": 2,
        },
        "GET",
        "/api/courtcases/special/082-CR-1/entities/",
        {"page": 2},
        None,
        id="court-case-entities-list",
    ),
    pytest.param(
        BrowseCourtDataTool(),
        {
            "action": "list_documents",
            "court": "special",
            "case_number": "082-CR-1",
        },
        "GET",
        "/api/courtcases/special/082-CR-1/documents/",
        None,
        None,
        id="court-documents-list",
    ),
    pytest.param(
        BrowseCourtDataTool(),
        {"action": "search_entities", "name": "Ram", "page": 4},
        "GET",
        "/api/courtcase-entities/",
        {"name": "Ram", "page": 4},
        None,
        id="court-entities-search",
    ),
    pytest.param(
        BrowseCourtDataTool(),
        {"action": "list_firms", "name": "Acme", "page": 2},
        "GET",
        "/api/firms/",
        {"name": "Acme", "page": 2},
        None,
        id="firms-list",
    ),
    pytest.param(
        BrowseCourtDataTool(),
        {"action": "get_firm", "firm_id": 7},
        "GET",
        "/api/firms/7/",
        None,
        None,
        id="firms-get",
    ),
    pytest.param(
        ManageCourtDataTool(),
        {
            "action": "create_court",
            "body": {
                "identifier": "special",
                "court_type": "special",
                "full_name_nepali": "Bises Adalat",
            },
        },
        "POST",
        "/api/courts/",
        None,
        {
            "identifier": "special",
            "court_type": "special",
            "full_name_nepali": "Bises Adalat",
        },
        id="courts-create",
    ),
    pytest.param(
        ManageCourtDataTool(),
        {
            "action": "update_court",
            "court": "special",
            "body": {"full_name_english": "Special Court"},
        },
        "PATCH",
        "/api/courts/special/",
        None,
        {"full_name_english": "Special Court"},
        id="courts-update",
    ),
    pytest.param(
        ManageCourtDataTool(),
        {
            "action": "create_case",
            "body": {"case_number": "082-CR-1", "court_identifier": "special"},
        },
        "POST",
        "/api/courtcases/",
        None,
        {"case_number": "082-CR-1", "court_identifier": "special"},
        id="court-cases-create",
    ),
    pytest.param(
        ManageCourtDataTool(),
        {
            "action": "update_case",
            "court": "special",
            "case_number": "082-CR-1",
            "body": {
                "case_number": "082-CR-1",
                "court_identifier": "special",
                "case_status": "decided",
            },
        },
        "PATCH",
        "/api/courtcases/special/082-CR-1/",
        None,
        {
            "case_number": "082-CR-1",
            "court_identifier": "special",
            "case_status": "decided",
        },
        id="court-cases-update",
    ),
    pytest.param(
        ManageCourtDataTool(),
        {
            "action": "delete_case",
            "court": "special",
            "case_number": "082-CR-1",
        },
        "DELETE",
        "/api/courtcases/special/082-CR-1/",
        None,
        None,
        id="court-cases-delete",
    ),
    pytest.param(
        ManageCourtDataTool(),
        {
            "action": "ingest_cases",
            "items": [{"court": "special", "case_number": "1"}],
        },
        "POST",
        "/api/ingestion/cases/",
        None,
        {"items": [{"court": "special", "case_number": "1"}]},
        id="court-cases-ingest",
    ),
    pytest.param(
        ManageCourtDataTool(),
        {
            "action": "resolve_entities",
            "items": [
                {
                    "court": "special",
                    "case_number": "1",
                    "nes_id": "https://jawafdehi.org/entity/person/ram",
                }
            ],
        },
        "POST",
        "/api/ingestion/entities/resolve/",
        None,
        {
            "items": [
                {
                    "court": "special",
                    "case_number": "1",
                    "nes_id": "https://jawafdehi.org/entity/person/ram",
                }
            ]
        },
        id="court-entities-resolve",
    ),
    pytest.param(
        ManageCourtDataTool(),
        {
            "action": "register_documents",
            "items": [
                {
                    "court": "special",
                    "case_number": "1",
                    "document_source": {},
                }
            ],
        },
        "POST",
        "/api/ingestion/documents/",
        None,
        {
            "items": [
                {
                    "court": "special",
                    "case_number": "1",
                    "document_source": {},
                }
            ]
        },
        id="court-documents-register",
    ),
    pytest.param(
        ManageCourtDataTool(),
        {
            "action": "ingest_firms",
            "items": [{"firm_name": "Acme", "blacklist_date_bs": "2080-01-01"}],
        },
        "POST",
        "/api/ingestion/firms/",
        None,
        {"items": [{"firm_name": "Acme", "blacklist_date_bs": "2080-01-01"}]},
        id="firms-ingest",
    ),
    pytest.param(
        ManageCourtDataTool(),
        {"action": "create_firm", "body": {"firm_name": "Acme"}},
        "POST",
        "/api/firms/",
        None,
        {"firm_name": "Acme"},
        id="firms-create",
    ),
    pytest.param(
        ManageCourtDataTool(),
        {
            "action": "update_firm",
            "firm_id": 7,
            "body": {"reason": "settled"},
        },
        "PATCH",
        "/api/firms/7/",
        None,
        {"reason": "settled"},
        id="firms-update",
    ),
    pytest.param(
        ManageCaseUpdateProposalsTool(),
        {"action": "list", "status": "pending", "page": 2},
        "GET",
        "/api/case-update-proposals/",
        {"status": "pending", "page": 2},
        None,
        id="proposals-list",
    ),
    pytest.param(
        ManageCaseUpdateProposalsTool(),
        {"action": "get", "proposal_id": 3},
        "GET",
        "/api/case-update-proposals/3/",
        None,
        None,
        id="proposals-get",
    ),
    pytest.param(
        ManageCaseUpdateProposalsTool(),
        {
            "action": "create",
            "body": {
                "case_slug": "case-a",
                "source_kind": "caseworker",
                "intent": {
                    "type": "append_timeline_entry",
                    "entry": {"date": "2026-01-01", "title": "Filed"},
                },
                "confidence": 1,
                "detected_by": "caseworker:test",
                "dedup_key": "case-a:filed",
            },
        },
        "POST",
        "/api/case-update-proposals/",
        None,
        {
            "case_slug": "case-a",
            "source_kind": "caseworker",
            "intent": {
                "type": "append_timeline_entry",
                "entry": {"date": "2026-01-01", "title": "Filed"},
            },
            "confidence": 1,
            "detected_by": "caseworker:test",
            "dedup_key": "case-a:filed",
        },
        id="proposals-create",
    ),
    pytest.param(
        ManageCaseUpdateProposalsTool(),
        {
            "action": "edit",
            "proposal_id": 3,
            "body": {
                "intent": {
                    "type": "link_material",
                    "material": "https://jawafdehi.org/material/court/order-1",
                }
            },
        },
        "PATCH",
        "/api/case-update-proposals/3/intent/",
        None,
        {
            "intent": {
                "type": "link_material",
                "material": "https://jawafdehi.org/material/court/order-1",
            }
        },
        id="proposals-edit",
    ),
    pytest.param(
        ManageCaseUpdateProposalsTool(),
        {"action": "approve", "proposal_id": 3, "body": {"notes": "ok"}},
        "POST",
        "/api/case-update-proposals/3/approve/",
        None,
        {"notes": "ok"},
        id="proposals-approve",
    ),
    pytest.param(
        ManageCaseUpdateProposalsTool(),
        {"action": "reject", "proposal_id": 3},
        "POST",
        "/api/case-update-proposals/3/reject/",
        None,
        {},
        id="proposals-reject",
    ),
    pytest.param(
        ManageCaseworkReviewsTool(),
        {"action": "list", "slug": "case-a", "page": 2},
        "GET",
        "/api/casework/reviews/",
        {"slug": "case-a", "page": 2},
        None,
        id="reviews-list",
    ),
    pytest.param(
        ManageCaseworkReviewsTool(),
        {"action": "list_grouped", "slug": "case-a", "page": 2},
        "GET",
        "/api/casework/reviews/grouped/",
        {"slug": "case-a", "page": 2},
        None,
        id="reviews-list-grouped",
    ),
    pytest.param(
        ManageCaseworkReviewsTool(),
        {"action": "get", "review_id": 4},
        "GET",
        "/api/casework/reviews/4/",
        None,
        None,
        id="reviews-get",
    ),
    pytest.param(
        ManageCaseworkReviewsTool(),
        {"action": "submit", "body": {"slug": "case-a"}},
        "POST",
        "/api/casework/reviews/submit/",
        None,
        {"slug": "case-a"},
        id="reviews-submit",
    ),
    pytest.param(
        ManageCaseworkReviewsTool(),
        {"action": "list_rules"},
        "GET",
        "/api/casework/rules/",
        None,
        None,
        id="review-rules-list",
    ),
    pytest.param(
        ManageCaseworkReviewsTool(),
        {"action": "get_rule", "rule_id": 2},
        "GET",
        "/api/casework/rules/2/",
        None,
        None,
        id="review-rules-get",
    ),
    pytest.param(
        ManageCaseworkReviewsTool(),
        {"action": "get_config"},
        "GET",
        "/api/casework/config/",
        None,
        None,
        id="review-config-get",
    ),
    pytest.param(
        ManageCaseworkReviewsTool(),
        {"action": "update_config", "body": {"pass_threshold": 85}},
        "PUT",
        "/api/casework/config/",
        None,
        {"pass_threshold": 85},
        id="review-config-update",
    ),
    pytest.param(
        ManageCaseworkReviewsTool(),
        {"action": "regrade_all"},
        "POST",
        "/api/casework/reviews/regrade-all/",
        None,
        {},
        id="reviews-regrade",
    ),
    pytest.param(
        ManageJobsTool(),
        {"action": "list", "kind": "case_review", "limit": 20},
        "GET",
        "/api/jobs/",
        {"kind": "case_review", "limit": 20},
        None,
        id="jobs-list",
    ),
    pytest.param(
        ManageJobsTool(),
        {"action": "enqueue", "body": {"kind": "case_review"}},
        "POST",
        "/api/jobs/",
        None,
        {"kind": "case_review"},
        id="jobs-enqueue",
    ),
    pytest.param(
        ManageJobsTool(),
        {"action": "claim", "body": {"kinds": ["case_review"]}},
        "POST",
        "/api/jobs/claim/",
        None,
        {"kinds": ["case_review"]},
        id="jobs-claim",
    ),
    pytest.param(
        ManageJobsTool(),
        {"action": "stage", "job_id": 9, "body": {"stage": "fetching"}},
        "POST",
        "/api/jobs/9/stage/",
        None,
        {"stage": "fetching"},
        id="jobs-stage",
    ),
    pytest.param(
        ManageJobsTool(),
        {
            "action": "result",
            "job_id": 9,
            "body": {"status": "done", "result": {"count": 1}},
        },
        "POST",
        "/api/jobs/9/result/",
        None,
        {"status": "done", "result": {"count": 1}},
        id="jobs-result",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "method", "path", "params", "body"),
    ACTION_ROUTE_CASES,
)
async def test_all_control_plane_actions_map_to_bounded_routes(
    tool, arguments, method, path, params, body
):
    Draft202012Validator(tool.input_schema).validate(arguments)
    request, payload = await _call(tool, arguments)

    request.assert_awaited_once_with(
        method,
        path,
        params=params,
        json_body=body,
        headers=None,
    )
    assert payload["success"] is True


@pytest.mark.asyncio
async def test_unified_search_maps_repeatable_filters():
    request, payload = await _call(
        SearchControlPlaneTool(),
        {
            "q": "court",
            "type": ["case", "courtcase"],
            "tags": ["procurement", "procurement-irregularity"],
            "page_size": 25,
        },
    )

    request.assert_awaited_once_with(
        "GET",
        "/api/search/",
        params={
            "q": "court",
            "type": ["case", "courtcase"],
            "tags": ["procurement", "procurement-irregularity"],
            "page_size": 25,
        },
        json_body=None,
        headers=None,
    )
    assert payload["success"] is True


@pytest.mark.asyncio
async def test_entity_history_encodes_full_iri_as_one_reference():
    request, _ = await _call(
        GetNESEntityVersionsTool(),
        {
            "entity_id": "https://jawafdehi.org/entity/person/a",
            "limit": 10,
        },
    )
    assert request.await_args.args == (
        "GET",
        "/api/entities/https%3A%2F%2Fjawafdehi.org%2Fentity%2Fperson%2Fa/versions",
    )
    assert request.await_args.kwargs["params"] == {"limit": 10}


@pytest.mark.asyncio
async def test_entity_delete_uses_control_plane_permission_boundary():
    request, _ = await _call(
        DeleteNESEntityTool(),
        {"entity_id": "person/ram"},
    )
    assert request.await_args.args == (
        "DELETE",
        "/api/entities/https%3A%2F%2Fjawafdehi.org%2Fentity%2Fperson%2Fram",
    )


@pytest.mark.asyncio
async def test_entity_reference_is_canonicalized_before_path_encoding():
    request, _ = await _call(
        GetNESEntityVersionsTool(),
        {"entity_id": "http://foreign.example/entity/person/ram"},
    )
    assert request.await_args.args == (
        "GET",
        "/api/entities/https%3A%2F%2Fjawafdehi.org%2Fentity%2Fperson%2Fram/versions",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool",
    [GetNESEntityVersionsTool(), DeleteNESEntityTool()],
    ids=lambda tool: tool.name,
)
async def test_invalid_entity_reference_returns_stable_tool_error(tool):
    result = await tool.execute({"entity_id": "missing-prefix"})

    assert result.is_error is True
    assert json.loads(result[0].text) == {
        "success": False,
        "error": "entity reference must be an IRI or prefix/slug.",
    }


@pytest.mark.asyncio
async def test_material_read_and_conditional_patch_mapping():
    read_request, _ = await _call(
        BrowseMaterialsTool(),
        {"action": "get", "iri": "https://jawafdehi.org/material/court/x"},
    )
    assert read_request.await_args.kwargs["params"] == {
        "iri": "https://jawafdehi.org/material/court/x"
    }

    patch_request, _ = await _call(
        ManageMaterialTool(),
        {
            "action": "patch",
            "iri": "https://jawafdehi.org/material/court/x",
            "patch_ops": [{"op": "add", "path": "/name", "value": "Order"}],
            "if_match": '"abc"',
        },
    )
    assert patch_request.await_args.args == ("PATCH", "/api/materials/")
    assert patch_request.await_args.kwargs["headers"] == {"If-Match": '"abc"'}
    assert patch_request.await_args.kwargs["json_body"]["patch_ops"][0]["op"] == ("add")


@pytest.mark.asyncio
async def test_court_browse_and_ingestion_mapping():
    browse_request, _ = await _call(
        BrowseCourtDataTool(),
        {
            "action": "list_hearings",
            "court": "special",
            "case_number": "082-CR-1",
        },
    )
    assert browse_request.await_args.args == (
        "GET",
        "/api/courtcases/special/082-CR-1/hearings/",
    )

    ingestion_request, _ = await _call(
        ManageCourtDataTool(),
        {"action": "register_documents", "items": [{"case_number": "1"}]},
    )
    assert ingestion_request.await_args.args == (
        "POST",
        "/api/ingestion/documents/",
    )
    assert ingestion_request.await_args.kwargs["json_body"] == {
        "items": [{"case_number": "1"}]
    }

    firms_request, _ = await _call(
        BrowseCourtDataTool(),
        {"action": "list_firms", "name": "builders", "nes_id": "entity-1"},
    )
    assert firms_request.await_args.kwargs["params"] == {
        "name": "builders",
        "nes_id": "entity-1",
    }


@pytest.mark.asyncio
async def test_firm_create_and_update_map_to_control_plane_routes():
    create_request, _ = await _call(
        ManageCourtDataTool(),
        {"action": "create_firm", "body": {"firm_name": "Acme"}},
    )
    assert create_request.await_args.args == ("POST", "/api/firms/")

    update_request, _ = await _call(
        ManageCourtDataTool(),
        {
            "action": "update_firm",
            "firm_id": 7,
            "body": {"reason": "settled"},
            "partial": False,
        },
    )
    assert update_request.await_args.args == ("PATCH", "/api/firms/7/")


def test_court_management_schema_does_not_advertise_put_semantics():
    assert "partial" not in ManageCourtDataTool().input_schema["properties"]


@pytest.mark.asyncio
async def test_proposal_review_and_job_actions_map_to_bounded_routes():
    proposal_request, _ = await _call(
        ManageCaseUpdateProposalsTool(),
        {"action": "approve", "proposal_id": 7, "body": {"notes": "verified"}},
    )
    assert proposal_request.await_args.args == (
        "POST",
        "/api/case-update-proposals/7/approve/",
    )

    reject_request, _ = await _call(
        ManageCaseUpdateProposalsTool(),
        {"action": "reject", "proposal_id": 8},
    )
    assert reject_request.await_args.args == (
        "POST",
        "/api/case-update-proposals/8/reject/",
    )
    assert reject_request.await_args.kwargs["json_body"] == {}

    review_request, _ = await _call(
        ManageCaseworkReviewsTool(),
        {"action": "submit", "body": {"slug": "case-a"}},
    )
    assert review_request.await_args.args == (
        "POST",
        "/api/casework/reviews/submit/",
    )

    job_request, _ = await _call(
        ManageJobsTool(),
        {
            "action": "result",
            "job_id": 9,
            "body": {"status": "done", "result": {"count": 1}},
        },
    )
    assert job_request.await_args.args == (
        "POST",
        "/api/jobs/9/result/",
    )


@pytest.mark.asyncio
async def test_action_validation_does_not_issue_request():
    request = AsyncMock()
    with patch(
        "jawafdehi_mcp.tools.control_plane.request_control_plane",
        new=request,
    ):
        result = await ManageMaterialTool().execute({"action": "patch", "iri": "x"})

    assert request.await_count == 0
    assert json.loads(result[0].text)["success"] is False
