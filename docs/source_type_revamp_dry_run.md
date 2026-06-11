# Source-Type Revamp — Dry-Run Notes (JAWA-2604 / PR #169)

Projection of what migration `0027_revamp_source_types` will do, computed by
running the shared classifier (`cases/services/source_classifier.py`) over the
**261 live production sources** pulled read-only from `portal.jawafdehi.org/api/sources/`
on 2026-06-11 (identity `fiddler`, Contributor/read-only).

This is a *projection*, not a DB write. Nothing here was applied to production.

## How to reproduce the dry run

```bash
# Apply the full chain on a scratch DB and watch the migration output:
python manage.py migrate cases
```

The migration (`0027`) re-classifies **every non-deleted source** (per the
approved "replace + data-migrate" plan), then `0028` updates the field choices.
It also re-roles every `.md` link to `MARKDOWN` (idempotent).

Once the data is classified, `source_type` is made **mandatory**: `0030`
backfills any residual NULL to `MISC` (defensive — there should be none after
`0027`), and `0031` sets the column `NOT NULL` with a `MISC` default. New
sources that omit `source_type` therefore default to `MISC` rather than NULL.

## OLD source_type distribution (n=261)
- `MEDIA_NEWS`: 101
- `OFFICIAL_GOVERNMENT`: 76
- `None`: 31
- `LEGAL_COURT_ORDER`: 18
- `LEGAL_PROCEDURAL`: 16
- `SOCIAL_MEDIA`: 9
- `OTHER_VISUAL`: 4
- `INTERNAL_CORPORATE`: 2
- `LEGISLATIVE_DOC`: 2
- `INVESTIGATIVE_REPORT`: 2

## NEW source_type distribution (projected)
- `NEWS`: 102
- `MISC`: 53
- `AG_ABHIYOG_PATRA`: 30
- `CIAA_PRESS_RELEASE`: 26
- `COURT_ORDER`: 21
- `LAW_OR_BILL`: 12
- `SOCIAL_MEDIA`: 10
- `COURT_FILING_OTHER`: 5
- `OAG_AUDIT_REPORT`: 2

**Rows whose source_type changes:** 254 / 261
**Mis-roled `.md` links → MARKDOWN:** 36

## OLD → NEW transitions (count)
- `MEDIA_NEWS` → `NEWS`: 98
- `OFFICIAL_GOVERNMENT` → `MISC`: 23
- `OFFICIAL_GOVERNMENT` → `AG_ABHIYOG_PATRA`: 22
- `OFFICIAL_GOVERNMENT` → `CIAA_PRESS_RELEASE`: 22
- `None` → `MISC`: 18
- `LEGAL_COURT_ORDER` → `COURT_ORDER`: 14
- `SOCIAL_MEDIA` → `SOCIAL_MEDIA`: 7
- `LEGAL_PROCEDURAL` → `AG_ABHIYOG_PATRA`: 5
- `OFFICIAL_GOVERNMENT` → `LAW_OR_BILL`: 5
- `LEGAL_PROCEDURAL` → `LAW_OR_BILL`: 5
- `None` → `COURT_ORDER`: 5
- `LEGAL_PROCEDURAL` → `MISC`: 5
- `OTHER_VISUAL` → `MISC`: 3
- `None` → `SOCIAL_MEDIA`: 3
- `OFFICIAL_GOVERNMENT` → `OAG_AUDIT_REPORT`: 2
- `MEDIA_NEWS` → `AG_ABHIYOG_PATRA`: 2
- `LEGAL_COURT_ORDER` → `NEWS`: 2
- `None` → `CIAA_PRESS_RELEASE`: 2
- `INTERNAL_CORPORATE` → `MISC`: 2
- `LEGISLATIVE_DOC` → `LAW_OR_BILL`: 2
- `None` → `NEWS`: 2
- `INVESTIGATIVE_REPORT` → `MISC`: 2
- `SOCIAL_MEDIA` → `CIAA_PRESS_RELEASE`: 1
- `LEGAL_COURT_ORDER` → `COURT_FILING_OTHER`: 1
- `OFFICIAL_GOVERNMENT` → `COURT_ORDER`: 1
- `LEGAL_PROCEDURAL` → `CIAA_PRESS_RELEASE`: 1
- `LEGAL_COURT_ORDER` → `AG_ABHIYOG_PATRA`: 1
- `MEDIA_NEWS` → `COURT_FILING_OTHER`: 1
- `SOCIAL_MEDIA` → `COURT_ORDER`: 1
- `None` → `COURT_FILING_OTHER`: 1
- `OFFICIAL_GOVERNMENT` → `COURT_FILING_OTHER`: 1
- `OTHER_VISUAL` → `COURT_FILING_OTHER`: 1

## Notes on the transitions

- **`MEDIA_NEWS` → `NEWS`** (98): straight rename of the genuine news bucket.
- **`OFFICIAL_GOVERNMENT` splits** into `AG_ABHIYOG_PATRA` (charge sheets),
  `CIAA_PRESS_RELEASE` (press releases), `OAG_AUDIT_REPORT`, and `MISC`. The
  ~23 → `MISC` are genuinely miscellaneous gov docs (bidding documents, cabinet
  decisions, prison/bank letters) with no clean new home — expected, not a loss
  of a meaningful prior label (the old label was itself a heuristic grab-bag).
- **`LEGAL_PROCEDURAL` / `LEGAL_COURT_ORDER`** map to `AG_ABHIYOG_PATRA` /
  `COURT_ORDER` / `COURT_FILING_OTHER` by document keyword.
- **36 `.md` links** currently stored as `PERMALINK`/`RAW` are corrected to
  `MARKDOWN`, which stops the review poller from appending duplicate
  conversions (it keys on a `MARKDOWN`-role link to detect existing markdown).

## Safety

- Read-only survey only; no production writes performed.
- Migration writes via `.update()` (bypasses full_clean) so a `NEWS` source
  missing `publication_date` doesn't block the maintenance write.
- `0027` is irreversible (reverse = noop); re-running forward is idempotent.
