"""JSON Schemas for the task-oriented control-plane tools.

The schemas are intentionally static so the MCP server can still run over stdio
without bootstrapping Django. Unit tests compare their field sets with the
authoritative DRF serializers to catch contract drift.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

Schema = dict[str, Any]
ActionContract = dict[str, Any]


def _object(
    properties: Mapping[str, Schema],
    *,
    required: Sequence[str] = (),
    additional_properties: bool = False,
    **constraints: Any,
) -> Schema:
    schema: Schema = {
        "type": "object",
        "properties": deepcopy(dict(properties)),
        "additionalProperties": additional_properties,
    }
    if required:
        schema["required"] = list(required)
    schema.update(constraints)
    return schema


def _nullable(schema: Schema) -> Schema:
    return {"anyOf": [deepcopy(schema), {"type": "null"}]}


def _string(
    *,
    max_length: int | None = None,
    min_length: int | None = None,
    format_: str | None = None,
    description: str | None = None,
) -> Schema:
    schema: Schema = {"type": "string"}
    if max_length is not None:
        schema["maxLength"] = max_length
    if min_length is not None:
        schema["minLength"] = min_length
    if format_ is not None:
        schema["format"] = format_
    if description is not None:
        schema["description"] = description
    return schema


def _array(
    items: Schema,
    *,
    min_items: int | None = None,
    max_items: int | None = None,
) -> Schema:
    schema: Schema = {"type": "array", "items": deepcopy(items)}
    if min_items is not None:
        schema["minItems"] = min_items
    if max_items is not None:
        schema["maxItems"] = max_items
    return schema


def _contract(
    *,
    required: Sequence[str] = (),
    properties: Mapping[str, Schema] | None = None,
    any_of_required: Sequence[Sequence[str]] = (),
) -> ActionContract:
    return {
        "required": tuple(required),
        "properties": deepcopy(dict(properties or {})),
        "any_of_required": tuple(tuple(group) for group in any_of_required),
    }


def _action_schema(
    actions: Sequence[str],
    properties: Mapping[str, Schema],
    contracts: Mapping[str, ActionContract],
) -> Schema:
    """Build a root schema whose required fields vary with ``action``."""
    if set(actions) != set(contracts):
        missing = sorted(set(actions) - set(contracts))
        extra = sorted(set(contracts) - set(actions))
        raise ValueError(
            f"Action contracts do not match actions (missing={missing}, extra={extra})."
        )

    root_properties = {
        "action": {"type": "string", "enum": list(actions)},
        **deepcopy(dict(properties)),
    }
    rules = []
    for action in actions:
        contract = contracts[action]
        required = list(contract["required"])
        refinements = contract["properties"]
        referenced = set(required) | set(refinements)
        referenced.update(
            name for group in contract["any_of_required"] for name in group
        )
        unknown = sorted(referenced - set(root_properties))
        if unknown:
            raise ValueError(f"{action} contract references unknown fields: {unknown}.")

        then: Schema = {}
        if required:
            then["required"] = required
        if refinements:
            then["properties"] = deepcopy(refinements)
        if contract["any_of_required"]:
            then["anyOf"] = [
                {"required": list(group)} for group in contract["any_of_required"]
            ]
        rules.append(
            {
                "if": {
                    "properties": {"action": {"const": action}},
                    "required": ["action"],
                },
                "then": then,
            }
        )

    schema = _object(root_properties, required=("action",))
    schema["allOf"] = rules
    return schema


JSON_PATCH_OPERATION_SCHEMA = _object(
    {
        "op": {
            "type": "string",
            "enum": ["add", "remove", "replace", "move", "copy", "test"],
        },
        "path": _string(),
        "from": _string(),
        "value": {},
    },
    required=("op", "path"),
)
JSON_PATCH_OPERATION_SCHEMA["allOf"] = [
    {
        "if": {
            "properties": {"op": {"enum": ["move", "copy"]}},
            "required": ["op"],
        },
        "then": {"required": ["from"]},
    },
    {
        "if": {
            "properties": {"op": {"enum": ["add", "replace", "test"]}},
            "required": ["op"],
        },
        "then": {"required": ["value"]},
    },
]
JSON_PATCH_SCHEMA = _array(
    JSON_PATCH_OPERATION_SCHEMA,
    min_items=1,
    max_items=100,
)

VISIBILITY_POLICY_SCHEMA = {
    "type": "string",
    "enum": ["PUBLIC", "CASE_GATED", "PRIVATE"],
}


def browse_materials_schema(actions: Sequence[str]) -> Schema:
    return _action_schema(
        actions,
        {
            "iri": _string(format_="uri"),
            "source": _string(),
            "material_type": _string(),
            "cursor": _string(),
        },
        {
            "list": _contract(),
            "get": _contract(required=("iri",)),
        },
    )


def manage_material_schema(actions: Sequence[str]) -> Schema:
    return _action_schema(
        actions,
        {
            "iri": _string(format_="uri"),
            "material": _object({}, additional_properties=True, minProperties=1),
            "material_type": _string(),
            "visibility_policy": VISIBILITY_POLICY_SCHEMA,
            "patch_ops": JSON_PATCH_SCHEMA,
            "if_match": _string(description="ETag returned by a prior material read."),
        },
        {
            "upsert": _contract(required=("material",)),
            "patch": _contract(
                required=("iri",),
                any_of_required=(("patch_ops",), ("visibility_policy",)),
            ),
            "delete": _contract(required=("iri",)),
        },
    )


COURT_BODY_PROPERTIES: dict[str, Schema] = {
    "identifier": _string(max_length=50),
    "court_type": _string(max_length=20),
    "full_name_nepali": _string(max_length=200),
    "full_name_english": _nullable(_string(max_length=200)),
}
COURT_CREATE_BODY_SCHEMA = _object(
    COURT_BODY_PROPERTIES,
    required=("identifier", "court_type", "full_name_nepali"),
)
COURT_UPDATE_BODY_SCHEMA = _object(COURT_BODY_PROPERTIES, minProperties=1)

COURT_CASE_BODY_PROPERTIES: dict[str, Schema] = {
    "case_number": _string(max_length=50),
    "court_identifier": _string(max_length=50),
    "registration_date_bs": _nullable(_string(max_length=20)),
    "registration_date_ad": _nullable(_string(format_="date")),
    "case_type": _nullable(_string(max_length=200)),
    "case_status": _nullable(_string(max_length=100)),
    "plaintiff": _nullable(_string()),
    "defendant": _nullable(_string()),
    "nes_id": _nullable(_string(format_="uri", max_length=300)),
    "extra_data": _nullable(_object({}, additional_properties=True)),
    "document_sources": _nullable(_array(_object({}, additional_properties=True))),
}
COURT_CASE_CREATE_BODY_SCHEMA = _object(
    COURT_CASE_BODY_PROPERTIES,
    required=("case_number", "court_identifier"),
)
COURT_CASE_UPDATE_BODY_SCHEMA = _object(
    COURT_CASE_BODY_PROPERTIES,
    minProperties=1,
)

FIRM_BODY_PROPERTIES: dict[str, Schema] = {
    "firm_name": _string(max_length=500),
    "proprietor_name": _nullable(_string(max_length=500)),
    "address": _nullable(_string(max_length=500)),
    "blacklist_date_bs": _nullable(_string(max_length=20)),
    "blacklist_date_ad": _nullable(_string(format_="date")),
    "effective_until_bs": _nullable(_string(max_length=20)),
    "effective_until_ad": _nullable(_string(format_="date")),
    "duration": _nullable(_string(max_length=100)),
    "reason": _nullable(_string()),
    "recommending_office": _nullable(_string(max_length=500)),
    "nes_id": _nullable(_string(format_="uri", max_length=300)),
}
FIRM_CREATE_BODY_SCHEMA = _object(FIRM_BODY_PROPERTIES, required=("firm_name",))
FIRM_UPDATE_BODY_SCHEMA = _object(FIRM_BODY_PROPERTIES, minProperties=1)

COURT_CASE_INGEST_ITEM_SCHEMA = _object(
    {
        **COURT_CASE_BODY_PROPERTIES,
        "court": _string(max_length=50),
    },
    required=("case_number",),
)
COURT_CASE_INGEST_ITEM_SCHEMA["anyOf"] = [
    {"required": ["court"]},
    {"required": ["court_identifier"]},
]

ENTITY_RESOLUTION_ITEM_SCHEMA = _object(
    {
        "court": _string(max_length=50),
        "court_identifier": _string(max_length=50),
        "case_number": _string(max_length=50),
        "nes_id": _string(format_="uri", max_length=300),
        "side": _string(max_length=20),
        "name": _string(max_length=500),
    },
    required=("case_number", "nes_id"),
)
ENTITY_RESOLUTION_ITEM_SCHEMA["anyOf"] = [
    {"required": ["court"]},
    {"required": ["court_identifier"]},
]

ROLED_LINK_SCHEMA = _object(
    {
        "link": _string(format_="uri"),
        "role": _string(),
    },
    required=("link", "role"),
    additional_properties=True,
)
DOCUMENT_SOURCE_SCHEMA = _object(
    {
        "document_id": _string(),
        "url": _array(ROLED_LINK_SCHEMA),
    },
    additional_properties=True,
)
DOCUMENT_INGEST_ITEM_SCHEMA = _object(
    {
        "court": _string(max_length=50),
        "court_identifier": _string(max_length=50),
        "case_number": _string(max_length=50),
        "document_source": DOCUMENT_SOURCE_SCHEMA,
    },
    required=("case_number", "document_source"),
)
DOCUMENT_INGEST_ITEM_SCHEMA["anyOf"] = [
    {"required": ["court"]},
    {"required": ["court_identifier"]},
]

FIRM_INGEST_ITEM_PROPERTIES = deepcopy(FIRM_BODY_PROPERTIES)
FIRM_INGEST_ITEM_PROPERTIES["blacklist_date_bs"] = _string(max_length=20)
FIRM_INGEST_ITEM_SCHEMA = _object(
    FIRM_INGEST_ITEM_PROPERTIES,
    required=("firm_name", "blacklist_date_bs"),
)


def browse_court_data_schema(actions: Sequence[str]) -> Schema:
    contracts = {
        "list_courts": _contract(),
        "get_court": _contract(required=("court",)),
        "list_cases": _contract(),
        "get_case": _contract(required=("court", "case_number")),
        "list_hearings": _contract(required=("court", "case_number")),
        "list_entities": _contract(required=("court", "case_number")),
        "list_documents": _contract(required=("court", "case_number")),
        "search_entities": _contract(),
        "list_firms": _contract(),
        "get_firm": _contract(required=("firm_id",)),
    }
    return _action_schema(
        actions,
        {
            "court": _string(),
            "case_number": _string(),
            "firm_id": {"type": "integer", "minimum": 1},
            "type": _string(),
            "status": _string(),
            "date_from": _string(format_="date"),
            "date_to": _string(format_="date"),
            "nes_id": _string(format_="uri"),
            "name": _string(),
            "page": {"type": "integer", "minimum": 1, "default": 1},
        },
        contracts,
    )


def manage_court_data_schema(actions: Sequence[str]) -> Schema:
    generic_body = _object({}, additional_properties=True)
    generic_items = _array(_object({}, additional_properties=True), max_items=500)
    contracts = {
        "create_court": _contract(
            required=("body",),
            properties={"body": COURT_CREATE_BODY_SCHEMA},
        ),
        "update_court": _contract(
            required=("court", "body"),
            properties={"body": COURT_UPDATE_BODY_SCHEMA},
        ),
        "create_case": _contract(
            required=("body",),
            properties={"body": COURT_CASE_CREATE_BODY_SCHEMA},
        ),
        "update_case": _contract(
            required=("court", "case_number", "body"),
            properties={"body": COURT_CASE_UPDATE_BODY_SCHEMA},
        ),
        "delete_case": _contract(required=("court", "case_number")),
        "ingest_cases": _contract(
            required=("items",),
            properties={"items": _array(COURT_CASE_INGEST_ITEM_SCHEMA, max_items=500)},
        ),
        "resolve_entities": _contract(
            required=("items",),
            properties={"items": _array(ENTITY_RESOLUTION_ITEM_SCHEMA, max_items=500)},
        ),
        "register_documents": _contract(
            required=("items",),
            properties={"items": _array(DOCUMENT_INGEST_ITEM_SCHEMA, max_items=500)},
        ),
        "ingest_firms": _contract(
            required=("items",),
            properties={"items": _array(FIRM_INGEST_ITEM_SCHEMA, max_items=500)},
        ),
        "create_firm": _contract(
            required=("body",),
            properties={"body": FIRM_CREATE_BODY_SCHEMA},
        ),
        "update_firm": _contract(
            required=("firm_id", "body"),
            properties={"body": FIRM_UPDATE_BODY_SCHEMA},
        ),
    }
    return _action_schema(
        actions,
        {
            "court": _string(),
            "case_number": _string(),
            "firm_id": {"type": "integer", "minimum": 1},
            "body": generic_body,
            "items": generic_items,
        },
        contracts,
    )


TIMELINE_ENTRY_SCHEMA = _object(
    {
        "date": _string(),
        "title": _string(min_length=1),
        "description": _string(),
        "date_bs": _string(),
        "end_date": _string(),
        "end_date_bs": _string(),
    },
    required=("date", "title"),
)
PROPOSAL_INTENT_SCHEMA: Schema = {
    "oneOf": [
        _object(
            {
                "type": {"const": "append_timeline_entry"},
                "entry": TIMELINE_ENTRY_SCHEMA,
            },
            required=("type", "entry"),
        ),
        _object(
            {
                "type": {"const": "link_material"},
                "material": _string(format_="uri"),
                "relation": _string(),
            },
            required=("type", "material"),
        ),
        _object(
            {
                "type": {"const": "raw_patch"},
                "patch": _array(
                    JSON_PATCH_OPERATION_SCHEMA,
                    min_items=1,
                    max_items=100,
                ),
            },
            required=("type", "patch"),
        ),
    ]
}
PROPOSAL_CREATE_BODY_PROPERTIES: dict[str, Schema] = {
    "case_slug": _string(max_length=50),
    "case_title": _string(max_length=200),
    "source_kind": {
        "type": "string",
        "enum": [
            "ngm_docket",
            "court_order",
            "ciaa_press",
            "news",
            "caseworker",
        ],
    },
    "intent": PROPOSAL_INTENT_SCHEMA,
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "source": _string(max_length=500),
    "detected_by": _string(max_length=100),
    "dedup_key": _string(max_length=300),
    "origin_subject": _string(max_length=100),
    "origin_msg_id": _string(max_length=100),
    "subject_refs": _array(_string()),
}
PROPOSAL_CREATE_BODY_SCHEMA = _object(
    PROPOSAL_CREATE_BODY_PROPERTIES,
    required=(
        "case_slug",
        "source_kind",
        "intent",
        "confidence",
        "detected_by",
        "dedup_key",
    ),
)
PROPOSAL_EDIT_BODY_SCHEMA = _object(
    {"intent": PROPOSAL_INTENT_SCHEMA},
    required=("intent",),
)
PROPOSAL_DECISION_BODY_SCHEMA = _object(
    {"notes": _string()},
)


def manage_case_update_proposals_schema(actions: Sequence[str]) -> Schema:
    contracts = {
        "list": _contract(),
        "get": _contract(required=("proposal_id",)),
        "create": _contract(
            required=("body",),
            properties={"body": PROPOSAL_CREATE_BODY_SCHEMA},
        ),
        "edit": _contract(
            required=("proposal_id", "body"),
            properties={"body": PROPOSAL_EDIT_BODY_SCHEMA},
        ),
        "approve": _contract(
            required=("proposal_id",),
            properties={"body": PROPOSAL_DECISION_BODY_SCHEMA},
        ),
        "reject": _contract(
            required=("proposal_id",),
            properties={"body": PROPOSAL_DECISION_BODY_SCHEMA},
        ),
    }
    return _action_schema(
        actions,
        {
            "proposal_id": {"type": "integer", "minimum": 1},
            "body": _object({}, additional_properties=True),
            "status": {
                "type": "string",
                "enum": ["pending", "approved", "rejected"],
            },
            "source_kind": deepcopy(PROPOSAL_CREATE_BODY_PROPERTIES["source_kind"]),
            "case_slug": _string(max_length=50),
            "page": {"type": "integer", "minimum": 1},
        },
        contracts,
    )


REVIEW_SUBMIT_BODY_SCHEMA = _object(
    {
        "iri": _string(format_="uri", max_length=300),
        "slug": _string(max_length=255),
    }
)
REVIEW_SUBMIT_BODY_SCHEMA["oneOf"] = [
    {"required": ["iri"]},
    {"required": ["slug"]},
]
REVIEW_CONFIG_BODY_PROPERTIES: dict[str, Schema] = {
    "pass_threshold": {"type": "integer"},
    "revise_threshold": {"type": "integer"},
    "llm_samples": {"type": "integer", "minimum": 1},
}
REVIEW_CONFIG_BODY_SCHEMA = _object(
    REVIEW_CONFIG_BODY_PROPERTIES,
    minProperties=1,
)


def manage_casework_reviews_schema(actions: Sequence[str]) -> Schema:
    contracts = {
        "list": _contract(),
        "list_grouped": _contract(),
        "get": _contract(required=("review_id",)),
        "submit": _contract(
            required=("body",),
            properties={"body": REVIEW_SUBMIT_BODY_SCHEMA},
        ),
        "list_rules": _contract(),
        "get_rule": _contract(required=("rule_id",)),
        "get_config": _contract(),
        "update_config": _contract(
            required=("body",),
            properties={"body": REVIEW_CONFIG_BODY_SCHEMA},
        ),
        "regrade_all": _contract(),
    }
    return _action_schema(
        actions,
        {
            "review_id": {"type": "integer", "minimum": 1},
            "rule_id": {"type": "integer", "minimum": 1},
            "slug": _string(),
            "page": {"type": "integer", "minimum": 1},
            "body": _object({}, additional_properties=True),
        },
        contracts,
    )


JOB_ENQUEUE_BODY_PROPERTIES: dict[str, Schema] = {
    "kind": _string(max_length=64),
    "payload": _object({}, additional_properties=True),
    "dedup_key": _nullable(_string(max_length=255)),
    "priority": {"type": "integer"},
}
JOB_ENQUEUE_BODY_SCHEMA = _object(
    JOB_ENQUEUE_BODY_PROPERTIES,
    required=("kind",),
)
JOB_CLAIM_BODY_SCHEMA = _object(
    {
        "kinds": _array(_string(max_length=64), min_items=1),
    },
    required=("kinds",),
)
JOB_STAGE_BODY_SCHEMA = _object(
    {"stage": _string(max_length=64)},
)
JOB_RESULT_BODY_SCHEMA = _object(
    {
        "status": {"type": "string", "enum": ["done", "failed"]},
        "result": {},
        "error": _string(),
        "retryable": {"type": "boolean", "default": False},
        "duration_seconds": _nullable({"type": "number", "minimum": 0}),
    },
    required=("status",),
)
JOB_RESULT_BODY_SCHEMA["allOf"] = [
    {
        "if": {
            "properties": {"status": {"const": "done"}},
            "required": ["status"],
        },
        "then": {
            "required": ["result"],
            "properties": {"result": {"not": {"type": "null"}}},
        },
    },
    {
        "if": {
            "properties": {"status": {"const": "failed"}},
            "required": ["status"],
        },
        "then": {
            "required": ["error"],
            "properties": {"error": _string(min_length=1)},
        },
    },
]


def manage_jobs_schema(actions: Sequence[str]) -> Schema:
    contracts = {
        "list": _contract(),
        "enqueue": _contract(
            required=("body",),
            properties={"body": JOB_ENQUEUE_BODY_SCHEMA},
        ),
        "claim": _contract(
            required=("body",),
            properties={"body": JOB_CLAIM_BODY_SCHEMA},
        ),
        "stage": _contract(
            required=("job_id", "body"),
            properties={"body": JOB_STAGE_BODY_SCHEMA},
        ),
        "result": _contract(
            required=("job_id", "body"),
            properties={"body": JOB_RESULT_BODY_SCHEMA},
        ),
    }
    return _action_schema(
        actions,
        {
            "job_id": {"type": "integer", "minimum": 1},
            "kind": _string(max_length=64),
            "status": {
                "type": "string",
                "enum": ["queued", "running", "done", "failed", "dead"],
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "body": _object({}, additional_properties=True),
        },
        contracts,
    )
