# Manual Test Guide: Caseworker PATCH API

This guide explains how to manually validate the new caseworker PATCH endpoint using Swagger UI or curl.

## Scope

Endpoint:
PATCH /api/caseworker/cases/{id}/

What this verifies:
- Auth and permission behavior
- JSON Patch operation handling
- Immutable field blocking
- Timeline and evidence updates
- Entity relation updates via ID lists
- No partial writes on invalid multi-operation payloads

## Prerequisites

1. Start from the Jawafdehi API repo root.
2. Install dependencies.
3. Run database migrations.
4. Create at least:
- one Contributor user
- one unassigned Contributor user
- one Admin user
- one Case in DRAFT state
- one or more JawafEntity rows for relation tests
5. Ensure token auth is enabled and obtain auth tokens for test users.

## Run Server

1. Export a database URL if needed.
2. Start the API server.

Example flow:
- poetry run python manage.py migrate
- poetry run python manage.py runserver

Open Swagger UI:
- http://localhost:8000/api/swagger/

## Prepare Test Data

1. Create a case and assign one Contributor as case contributor.
2. Note the numeric case id.
3. Create three entities and note their numeric IDs:
- alleged entity id
- related entity id
- location entity id

## Auth Header for curl

Use this format:
Authorization: Token YOUR_TOKEN

## Test 1: Unauthorized Request Returns 401

Request:
PATCH /api/caseworker/cases/{id}/
Body:
[
  {
    "op": "replace",
    "path": "/title",
    "value": "Unauthorized edit"
  }
]

Expected:
- Status 401

## Test 2: Unassigned Contributor Returns 403

Use a valid token for a Contributor not assigned to the case.

Body:
[
  {
    "op": "replace",
    "path": "/title",
    "value": "Should fail"
  }
]

Expected:
- Status 403

## Test 3: Assigned Contributor Can Update Scalar Field

Body:
[
  {
    "op": "replace",
    "path": "/title",
    "value": "Updated title from manual test"
  }
]

Expected:
- Status 200
- Response title is updated
- Refresh case in admin or query API and verify persistence

## Test 4: Timeline Array Operations

Replace a nested value:
[
  {
    "op": "replace",
    "path": "/timeline/0/title",
    "value": "Timeline title updated"
  }
]

Append a new item:
[
  {
    "op": "add",
    "path": "/timeline/-",
    "value": {
      "date": "2026-03-14",
      "title": "Manual append event",
      "description": "Added during manual test"
    }
  }
]

Remove an item:
[
  {
    "op": "remove",
    "path": "/timeline/1"
  }
]

Expected:
- Status 200 for each valid operation
- Timeline reflects each change exactly

## Test 5: Replace Evidence List

Body:
[
  {
    "op": "replace",
    "path": "/evidence",
    "value": [
      {
        "source_id": "manual-src-001",
        "description": "Manual evidence replacement"
      }
    ]
  }
]

Expected:
- Status 200
- Response evidence matches payload
- Persistence confirmed on reload

## Test 6: Update Entity Relations by ID Lists

Body:
[
  {
    "op": "replace",
    "path": "/alleged_entity_ids",
    "value": [1, 2]
  },
  {
    "op": "replace",
    "path": "/related_entity_ids",
    "value": [3]
  },
  {
    "op": "replace",
    "path": "/location_ids",
    "value": [4]
  }
]

Replace IDs with real entity IDs from your test database.

Expected:
- Status 200
- Response entity arrays reflect updated relationships

## Test 7: Immutable Paths Are Rejected with 422

Try patching each blocked path and verify 422:
- /state
- /case_id
- /case_type
- /version
- /id
- /contributors
- /created_at
- /updated_at
- /versionInfo

Example:
[
  {
    "op": "replace",
    "path": "/case_type",
    "value": "CORRUPTION"
  }
]

Expected:
- Status 422
- Field remains unchanged in persisted case

## Test 8: Malformed JSON Patch Returns 400

Send a non-array body, for example:
{
  "op": "replace",
  "path": "/title",
  "value": "invalid format"
}

Expected:
- Status 400

## Test 9: Invalid Patch Pointer Returns 400

Body:
[
  {
    "op": "remove",
    "path": "/timeline/99"
  }
]

Expected:
- Status 400

## Test 10: Invalid Entity ID Returns 422

Body:
[
  {
    "op": "replace",
    "path": "/alleged_entity_ids",
    "value": [999999]
  }
]

Expected:
- Status 422

## Test 11: No Partial Writes on Mixed Multi-Op Payload

Send one valid op and one invalid op in the same document:
[
  {
    "op": "replace",
    "path": "/title",
    "value": "Transient value"
  },
  {
    "op": "replace",
    "path": "/state",
    "value": "PUBLISHED"
  }
]

Expected:
- Status 422
- Title remains unchanged in persisted data

## Optional: curl Example

Replace placeholders before running:
- CASE_ID
- TOKEN

curl -X PATCH http://localhost:8000/api/caseworker/cases/CASE_ID/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Token TOKEN" \
  -d '[{"op":"replace","path":"/title","value":"Curl updated title"}]'

Expected:
- Status 200
- Title updated

## Pass Criteria

Manual validation is complete when:
1. All success-path tests return 200 and persist correctly.
2. Permission and auth tests return expected 401 or 403.
3. Immutable and invalid payload cases return 422 or 400 as appropriate.
4. Multi-op failure cases do not partially persist data.
