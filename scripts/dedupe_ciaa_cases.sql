-- ==============================================================================
-- Dedupe Audit: Find and resolve duplicate DRAFT corruption cases
-- ==============================================================================
-- This script identifies duplicate DRAFT corruption cases created by
-- incompatible dedup strategies across three independent pipelines:
--   1. import_ciaa_cases (dedup by court_cases JSONB containment)
--   2. discover_and_draft_cases (dedup by title substring match)
--   3. jawafdehi-agents workflow (dedup by API search)
--
-- Run with: psql -f scripts/dedupe_ciaa_cases.sql
-- ==============================================================================

-- 1. Find duplicates grouped by court_cases special:* ref
--    Cases with the same special:* court case ref are duplicates.
WITH dupes AS (
    SELECT
        c.id,
        c.case_id,
        c.title,
        c.state,
        c.created_at,
        c.court_cases,
        c.ciaa_case_number,
        -- Extract the primary special:* ref for grouping
        jsonb_array_elements_text(c.court_cases) AS court_ref
    FROM cases_case c
    WHERE c.case_type = 'CORRUPTION'
      AND c.state = 'DRAFT'
      AND c.court_cases IS NOT NULL
      AND jsonb_array_length(c.court_cases) > 0
)
SELECT
    court_ref,
    COUNT(*) AS dup_count,
    array_agg(case_id ORDER BY created_at) AS case_ids,
    min(created_at) AS earliest,
    max(created_at) AS latest
FROM dupes
WHERE court_ref LIKE 'special:%'
GROUP BY court_ref
HAVING COUNT(*) > 1
ORDER BY dup_count DESC, court_ref;

-- 2. Summary statistics
SELECT
    COUNT(*) AS total_draft_corruption_cases,
    COUNT(DISTINCT ciaa_case_number) AS distinct_ciaa_refs,
    COUNT(*) - COUNT(DISTINCT ciaa_case_number) AS estimated_duplicates
FROM cases_case
WHERE case_type = 'CORRUPTION'
  AND state = 'DRAFT';

-- 3. Flag cases where ciaa_case_number is null but court_cases has a special:* ref
--    (these are cases that need backfill from court_cases)
SELECT
    c.case_id,
    c.title,
    c.created_at,
    c.court_cases
FROM cases_case c
WHERE c.case_type = 'CORRUPTION'
  AND c.state = 'DRAFT'
  AND c.ciaa_case_number IS NULL
  AND c.court_cases IS NOT NULL
  AND jsonb_array_length(c.court_cases) > 0
  AND EXISTS (
      SELECT 1 FROM jsonb_array_elements_text(c.court_cases) AS ref
      WHERE ref LIKE 'special:%'
  )
ORDER BY c.created_at;

-- ==============================================================================
-- Dedupe resolution strategy (manual review recommended):
--   1. For each duplicate group, keep the oldest case (has most complete data
--      as it was likely enriched first).
--   2. Merge evidence/timeline arrays from newer duplicates into the kept case.
--   3. Set the newer duplicates to CLOSED state with a note linking to kept case.
--   4. Backfill ciaa_case_number on cases that have court_cases but null.
-- ==============================================================================
