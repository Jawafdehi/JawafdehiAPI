#!/bin/bash
set -euo pipefail

# Manual smoke test for POST /api/cases/
#
# What it does:
# 1. Starts the local Django dev server in the background
# 2. Fetches or creates a DRF token for the existing `contributor` user
# 3. Creates a new draft case via POST /api/cases/
# 4. Verifies the persisted row via Django shell
#
# Requirements:
# - Run from any location; the script resolves the service root automatically
# - The `contributor` user must already exist
# - Poetry dependencies and migrations must already be in place

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8010}"
BASE_URL="http://${HOST}:${PORT}"
USERNAME="${USERNAME:-contributor}"

SERVER_LOG="$(mktemp -t jawafdehi-post-api-server.XXXXXX.log)"
RESPONSE_BODY="$(mktemp -t jawafdehi-post-api-response.XXXXXX.json)"

cleanup() {
    local exit_code=$?

    if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
        kill "$SERVER_PID" >/dev/null 2>&1 || true
        wait "$SERVER_PID" >/dev/null 2>&1 || true
    fi

    rm -f "$RESPONSE_BODY"

    if [[ $exit_code -eq 0 ]]; then
        rm -f "$SERVER_LOG"
    else
        echo
        echo "Server log retained at: $SERVER_LOG"
    fi
}

trap cleanup EXIT

cd "$SERVICE_ROOT"

echo "Starting Django server on ${BASE_URL} ..."
poetry run python manage.py runserver "${HOST}:${PORT}" --noreload >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

echo "Waiting for server to become ready ..."
for _ in $(seq 1 60); do
    if curl -fsS "$BASE_URL/api/schema/" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if ! curl -fsS "$BASE_URL/api/schema/" >/dev/null 2>&1; then
    echo "Server did not become ready in time."
    exit 1
fi

echo "Generating token for user '${USERNAME}' ..."
TOKEN="$({
    poetry run python manage.py shell -c '
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token

User = get_user_model()
user = User.objects.filter(username="'"$USERNAME"'").first()
if user is None:
    raise SystemExit("User not found: '"$USERNAME"'")

token, _ = Token.objects.get_or_create(user=user)
print(token.key)
'
} | tail -n 1)"

if [[ -z "$TOKEN" ]]; then
    echo "Failed to resolve token for user '${USERNAME}'."
    exit 1
fi

TIMESTAMP="$(date +%s)"
CASE_TITLE="Manual POST API smoke test ${TIMESTAMP}"

read -r -d '' PAYLOAD <<EOF || true
{
  "title": "${CASE_TITLE}",
  "case_type": "CORRUPTION",
  "short_description": "Created by manual POST API smoke test",
  "description": "This case was created by scripts/manual_test_case_post_api.sh",
  "tags": ["manual-test", "post-api"],
  "key_allegations": ["Manual API allegation ${TIMESTAMP}"],
  "timeline": [
    {
      "date": "2026-03-16",
      "title": "Manual smoke test execution",
      "description": "Created through authenticated POST /api/cases/"
    }
  ],
  "evidence": [
    {
      "source_id": "manual-smoke-${TIMESTAMP}",
      "description": "Synthetic evidence entry for manual API validation"
    }
  ]
}
EOF

echo "Creating case via POST ${BASE_URL}/api/cases/ ..."
HTTP_STATUS="$({
    curl -sS -o "$RESPONSE_BODY" -w "%{http_code}" \
        -X POST "${BASE_URL}/api/cases/" \
        -H "Content-Type: application/json" \
        -H "Authorization: Token ${TOKEN}" \
        -d "$PAYLOAD"
})"

echo "HTTP status: ${HTTP_STATUS}"
cat "$RESPONSE_BODY"
echo

if [[ "$HTTP_STATUS" != "201" ]]; then
    echo "Case creation failed."
    exit 1
fi

CASE_DB_ID="$(poetry run python - <<'PY' "$RESPONSE_BODY"
import json
import sys

with open(sys.argv[1], 'r', encoding='utf-8') as fh:
    data = json.load(fh)

print(data['id'])
PY
)"

CASE_PUBLIC_ID="$(poetry run python - <<'PY' "$RESPONSE_BODY"
import json
import sys

with open(sys.argv[1], 'r', encoding='utf-8') as fh:
    data = json.load(fh)

print(data['case_id'])
PY
)"

echo "Verifying persisted record via Django shell ..."
poetry run python manage.py shell -c '
from django.contrib.auth import get_user_model
from cases.models import Case, CaseState

User = get_user_model()
username = "'"$USERNAME"'"
case_id = int("'"$CASE_DB_ID"'")
expected_title = "'"$CASE_TITLE"'"

user = User.objects.get(username=username)
case = Case.objects.get(pk=case_id)

assert case.title == expected_title, f"Unexpected title: {case.title!r}"
assert case.state == CaseState.DRAFT, f"Unexpected state: {case.state!r}"
assert case.contributors.filter(pk=user.pk).exists(), "Creator was not added as contributor"

print("Database verification passed")
print(f"  id={case.id}")
print(f"  case_id={case.case_id}")
print(f"  state={case.state}")
print(f"  contributor_count={case.contributors.count()}")
'

echo
echo "Manual POST API smoke test passed."
echo "Created case pk: ${CASE_DB_ID}"
echo "Created case_id: ${CASE_PUBLIC_ID}"
