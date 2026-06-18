#!/usr/bin/env bash
#
# Run the casework enrichers in dependency order and collect per-enricher logs.
#
# Order matters: description runs LAST because it reads key_allegations /
# timeline / entities produced by the earlier passes.
#
# Usage:
#   JAWAFDEHI_API_TOKEN=...  JAWAFDEHI_LLM_API_KEY=...  casework/run_enrichment.sh [--apply] [extra args]
#
# Defaults: dry-run, --priority selection, proxy provider, model casework-only.
# Anything after the flags is passed verbatim to every enricher
# (e.g. --limit 3, --force, --verbose).
#
# Env overrides:
#   PYTHON                interpreter (default: ./.venv/bin/python)
#   JAWAFDEHI_API_BASE_URL  default https://portal.jawafdehi.org/api
#   PROVIDER              default proxy
#   MODEL                 default casework-only
#   ENRICH_SELECT         selection passed to each enricher (default "--priority")
#
set -uo pipefail

usage() { sed -n '2,/^set /p' "$0" | sed 's/^#//;s/^ //'; exit "${1:-0}"; }

# ── config ────────────────────────────────────────────────────────────────
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root (parent of casework/)
cd "$HERE"

PYTHON="${PYTHON:-$HERE/.venv/bin/python}"
API_BASE_URL="${JAWAFDEHI_API_BASE_URL:-https://portal.jawafdehi.org/api}"
PROVIDER="${PROVIDER:-proxy}"
MODEL="${MODEL:-casework-only}"
SELECT="${ENRICH_SELECT:---priority}"

# ── args ──────────────────────────────────────────────────────────────────
APPLY=0
EXTRA=()
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    -h|--help) usage 0 ;;
    *) EXTRA+=("$a") ;;
  esac
done
DRY="--dry-run"; MODE="DRY-RUN"
if [[ $APPLY -eq 1 ]]; then DRY=""; MODE="APPLY"; fi

# ── preflight ─────────────────────────────────────────────────────────────
: "${JAWAFDEHI_API_TOKEN:?set JAWAFDEHI_API_TOKEN}"
: "${JAWAFDEHI_LLM_API_KEY:?set JAWAFDEHI_LLM_API_KEY}"
export JAWAFDEHI_API_TOKEN JAWAFDEHI_LLM_API_KEY
[[ -x "$PYTHON" ]] || { echo "interpreter not found: $PYTHON (set PYTHON=...)" >&2; exit 1; }

# Dependency order — description LAST.
ENRICHERS=(allegations related_entities timeline missing_bigo tags description)

RUN_DIR="$HERE/logs/enrich-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN_DIR"
MASTER="$RUN_DIR/run.log"

{
  echo "mode:      $MODE"
  echo "select:    $SELECT ${EXTRA[*]:-}"
  echo "provider:  $PROVIDER | model: $MODEL"
  echo "api:       $API_BASE_URL"
  echo "logs:      $RUN_DIR"
  echo "started:   $(date -u +%FT%TZ)"
} | tee "$MASTER"

# ── run ───────────────────────────────────────────────────────────────────
declare -a RESULTS
for e in "${ENRICHERS[@]}"; do
  log="$RUN_DIR/${e}.log"
  echo "" | tee -a "$MASTER"
  echo "===== enrich_${e} ($MODE) =====" | tee -a "$MASTER"
  # -u: unbuffered so the per-line timestamps are real-time, not flushed in a clump.
  "$PYTHON" -u "casework/enrich_${e}.py" \
      $SELECT $DRY "${EXTRA[@]:-}" \
      --provider "$PROVIDER" --model "$MODEL" \
      --api-base-url "$API_BASE_URL" --api-token "$JAWAFDEHI_API_TOKEN" 2>&1 \
    | grep --line-buffered -vE "langgraph|allowed_objects|JsonPlus" \
    | while IFS= read -r line; do printf '%s | %s\n' "$(date +%H:%M:%S)" "$line"; done \
    | tee "$log" | tee -a "$MASTER"
  rc=${PIPESTATUS[0]}
  RESULTS+=("$e $rc")
  echo "----- enrich_${e} exit=$rc -----" | tee -a "$MASTER"
done

# ── summary ───────────────────────────────────────────────────────────────
echo "" | tee -a "$MASTER"
echo "================ SUMMARY ($MODE) ================" | tee -a "$MASTER"
printf "%-18s %4s  %s\n" "enricher" "exit" "processed/enriched/no-content/llm-error" | tee -a "$MASTER"
for r in "${RESULTS[@]}"; do
  e="${r% *}"; rc="${r#* }"; log="$RUN_DIR/${e}.log"
  proc=$(grep -aoE "Cases processed +[0-9]+"  "$log" | grep -oE "[0-9]+$" | tail -1)
  enr=$(grep -aoE  "Cases enriched +[0-9]+"   "$log" | grep -oE "[0-9]+$" | tail -1)
  noc=$(grep -aoE  "Cases no content +[0-9]+" "$log" | grep -oE "[0-9]+$" | tail -1)
  err=$(grep -aoE  "Cases llm error +[0-9]+"  "$log" | grep -oE "[0-9]+$" | tail -1)
  printf "%-18s %4s  %s/%s/%s/%s\n" "$e" "$rc" "${proc:-?}" "${enr:-?}" "${noc:-?}" "${err:-?}" | tee -a "$MASTER"
done
echo "finished:  $(date -u +%FT%TZ)" | tee -a "$MASTER"
echo "logs in:   $RUN_DIR" | tee -a "$MASTER"

# Nonzero exit if any enricher failed.
for r in "${RESULTS[@]}"; do [[ "${r#* }" -ne 0 ]] && exit 1; done
exit 0
