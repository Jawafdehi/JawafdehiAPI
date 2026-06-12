#!/usr/bin/env bash
#
# Reprocess document-source markdown against PRODUCTION (portal.jawafdehi.org).
#
# This runs the `reprocess_source_markdown` management command as a remote HTTP
# client (like the poller): it READS published cases from the public Jawafdehi
# API and WRITES converted markdown back to the casework API. It converts each
# source's primary link via likhit/MarkItDown (+ Bedrock OCR for scanned PDFs,
# + trafilatura main-content for web pages) and attaches a MARKDOWN-role url.
#
# WHAT THIS TOUCHES:
#   - WRITES to PRODUCTION: attaches/replaces MARKDOWN urls on DocumentSources
#     (only when NOT --dry-run). This is the intended effect.
#   - Spends AWS Bedrock (likhit OCR) per scanned source.
#   - Does NOT touch any database directly; the DATABASE_URL below is a throwaway
#     sqlite file only so Django can boot.
#
# Secrets (Bedrock/OCR creds, the prod token) are read from the local,
# gitignored backend/.env so they are not duplicated here.
#
# USAGE:
#   run/reprocess_prod.sh --dry-run                 # READ-ONLY: convert + report, no writes
#   run/reprocess_prod.sh --dry-run --slug <slug>   # dry-run a single case
#   run/reprocess_prod.sh --overwrite               # LIVE: re-convert + REPLACE all markdown
#   run/reprocess_prod.sh --overwrite --slug <slug> # LIVE: one case
#
# No flags are forced: pass --dry-run yourself to preview. A bare invocation
# would run LIVE in skip-existing mode, so ALWAYS dry-run first.
#
# NOTE: --overwrite re-downloads and re-converts EVERY source (re-runs OCR), so a
# full-corpus run is long and spends Bedrock per scanned doc. It is the right
# mode after a converter change (these source-link/HTML/role fixes) because it
# refreshes sources that already have markdown.
#
set -euo pipefail

# Run from THIS checkout (the branch with the reprocess command + converter fixes).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

ENV_FILE="/damodaha-volunteer/backend/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found (needed for Bedrock/OCR creds + prod token)." >&2
  exit 1
fi

# Read a single KEY from the .env WITHOUT shell-sourcing it (it is a
# python-dotenv file with values that are not valid shell, e.g. regexes).
envget() {
  local key="$1" line
  line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n1)" || true
  line="${line#${key}=}"
  line="${line%\"}"; line="${line#\"}"
  line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

# --- READ target: PRODUCTION public API (PUBLISHED cases) ---
export JAWAFDEHI_API_BASE="$(envget JAWAFDEHI_API_BASE)"
[[ -z "$JAWAFDEHI_API_BASE" ]] && export JAWAFDEHI_API_BASE="https://portal.jawafdehi.org/api"
export JAWAFDEHI_API_TOKEN="$(envget JAWAFDEHI_API_TOKEN)"
export REVIEW_CASE_SOURCE="remote"

# --- WRITE target: PRODUCTION casework API ---
export CASEWORK_API_BASE="https://portal.jawafdehi.org/api/casework"
# Attaching markdown requires a token for a service account with
# CanManageDocumentSources. Reuse the prod 'fiddler' token (JAWAFDEHI_API_TOKEN);
# allow an explicit override if one is already exported.
if [[ -z "${CASEWORK_POLLER_TOKEN:-}" ]]; then
  export CASEWORK_POLLER_TOKEN="$(envget JAWAFDEHI_API_TOKEN)"
fi
if [[ -z "${CASEWORK_POLLER_TOKEN}" ]]; then
  echo "ERROR: no prod token (JAWAFDEHI_API_TOKEN) found in $ENV_FILE." >&2
  exit 1
fi

# --- likhit OCR (Bedrock) + creds from .env ---
export BEDROCK_MODEL_ID="$(envget BEDROCK_MODEL_ID)"
export AWS_REGION="$(envget AWS_REGION)"
export AWS_CONFIG_FILE="/home/damodaha/.aws/config"
export AWS_PROFILE="$(envget REVIEW_AWS_PROFILE)"; [[ -z "$AWS_PROFILE" ]] && export AWS_PROFILE="orion-admin"
export REVIEW_AWS_PROFILE="$AWS_PROFILE"
export OPENAI_BASE_URL="$(envget OPENAI_BASE_URL)"
export OPENAI_API_KEY="$(envget OPENAI_API_KEY)"
export MARKITDOWN_OCR_MODEL="$(envget MARKITDOWN_OCR_MODEL)"
# Render scanned PDF pages at a Bedrock-payload-safe DPI for OCR.
export LIKHIT_OCR_DPI="${LIKHIT_OCR_DPI:-150}"

# Legacy .doc via LibreOffice stays OFF in prod (not in the image); .doc that
# antiword can't read will error-and-skip cleanly.
export LIBREOFFICE_DOC_CONVERSION="${LIBREOFFICE_DOC_CONVERSION:-false}"

# --- Django bootstrap: throwaway sqlite so settings load; NEVER the prod RDS ---
export SECRET_KEY="$(envget SECRET_KEY)"; [[ -z "$SECRET_KEY" ]] && export SECRET_KEY="reprocess-only-not-prod-secret"
export DATABASE_URL="sqlite:////tmp/reprocess_prod_boot.sqlite3"
export NGM_DATABASE_URL=""
export DATABASE_PASSWORD=""
export ALLOWED_HOSTS="localhost,127.0.0.1,testserver"

# --- Conversion cache + per-source timeout ---
# Persisted cache dir so a re-run reuses prior conversions (overwrite still
# re-converts). Generous timeout because multi-page OCR is slow.
export SOURCE_MARKDOWN_DIR="${SOURCE_MARKDOWN_DIR:-/tmp/reprocess_prod_cache}"
mkdir -p "$SOURCE_MARKDOWN_DIR"
export CONVERT_SOURCE_TIMEOUT="${CONVERT_SOURCE_TIMEOUT:-300}"

# Pace reads to avoid the prod API's rate limiter (HTTP 429).
READ_SLEEP="${READ_SLEEP:-0.5}"
# Pace per-case work (writes) to be gentle on prod.
CASE_SLEEP="${CASE_SLEEP:-0.5}"

VENV_PY=".venv/bin/python"
[[ -x "$VENV_PY" ]] || VENV_PY="/damodaha-volunteer/backend/.venv/bin/python"

echo "Reprocessing PRODUCTION document-source markdown:"
echo "  read : $JAWAFDEHI_API_BASE"
echo "  write: $CASEWORK_API_BASE"
echo "  ocr  : profile=$AWS_PROFILE model=${MARKITDOWN_OCR_MODEL:-unset} dpi=$LIKHIT_OCR_DPI"
echo "  cache: $SOURCE_MARKDOWN_DIR  timeout=${CONVERT_SOURCE_TIMEOUT}s"
echo "  args : $*"
echo

exec "$VENV_PY" manage.py reprocess_source_markdown \
  --read-sleep "$READ_SLEEP" --sleep "$CASE_SLEEP" "$@"
