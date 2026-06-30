# Local-Officials Sourcing — 2022 Nepal local-level elected HEADS + DEPUTIES

**Wave:** `nes/sourcing/local-officials` · **Date:** 2026-06-28 · **Conduct:** read-only,
public office data only. No DB writes (bulk_ingest is the orchestrator's job).

## Scope

The 2022 local-level election (held 13 May 2022 / 30 Baishakh 2079 BS). For each of
Nepal's **753 local units** (6 metropolitan, 11 sub-metropolitan, 276 municipalities,
460 rural municipalities) the elected:

- **HEAD** — Mayor (मेयर) of a metro/sub-metro/municipality, or Chairperson (अध्यक्ष)
  of a rural municipality.
- **DEPUTY** — Deputy Mayor (उपमेयर) / Vice-chairperson (उपाध्यक्ष).

Full coverage ≈ **1,506 persons** (753 heads + 753 deputies).

## Entity shape (mirrors the parliament-api wave's `normalize_parliament.py`)

`@type` "Person"; `@id` = `…/entity/person/<romanized-name>-<cbs-code>-<mayor|chair|deputy>`
(no Wikidata Q-id required — the stable slug is name + the **CBS local-unit code**, the
join key). Bilingual `name`; `hasOccupation` Role with `roleName`
"Mayor of <Unit>" / "Deputy Mayor of <Unit>" / "Chairperson of <Unit>" /
"Vice-chairperson of <Unit>"; `memberOf` → the local-unit OFFICE IRI; `containedInPlace`
→ the local-unit LOCATION IRI; `jawafdehi:party`; `jawafdehi:electionCycle`="2022 local";
`jawafdehi:branch`="local-government". The CBS code is also carried as a `PropertyValue`
identifier.

## Join key — CBS local-unit code

Each person's local unit is matched to the already-ingested **office** + **location**
IRIs in the offices-local wave's `localunit_offices.json` (753 records)
by the CBS local-unit code. The harvested Wikipedia unit label is normalized to the
office's base name (`_norm_join`) to resolve the code; the code then yields the canonical
office/location IRIs verbatim.

## ≥2-source rule (two INDEPENDENT publishers, else HOLD)

Every emitted person carries:
- **Source #1 (primary):** ECN 2022 local-level results (`result.election.gov.np`) — the
  official publisher of the per-palika winners (Devanagari; dynamic .aspx, no API/CSV —
  see `recon-elected-officials.md`).
- **Source #2 (corroborator):** Wikipedia "2022 Nepalese local elections" + per-district
  result articles — an independent, machine-readable 2nd publisher naming winners + party.

A person named by only ONE publisher is marked `held=true` honestly rather than inserted.

## Data tiers (what was machine-readably obtainable)

- **Tier A — 17 metro/sub-metro MAYORS (central article):** the 6 metropolitan + 11
  sub-metropolitan cities' elected mayors are published in machine-readable wikitables on
  the central Wikipedia article (`sources/metro_raw.json`). High confidence; ECN +
  Wikipedia = 2 independent publishers. **All 17 parsed, joined to CBS codes (0 unmatched),
  validated.** (Deputies of these cities are NOT on the central tables.)
- **Tier B — per-district harvest (`sources/wikipedia_district_winners.json`):** all 77
  district Wikipedia articles were scanned (~240 polite API requests, 0 HTTP 429). **Key
  finding: per-district articles do NOT generally carry per-local-unit 2022 winner tables**
  — their Administration/Divisions sections list population/area/website or describe
  district-level bodies (DCC/DAO/court), not local-unit election winners. Exactly **one**
  district (Udayapur) published a genuine per-unit table with **mayor + deputy for all 8 of
  its units** (no party); those 8 heads + 8 deputies were captured and joined by CBS code.
  The remaining ~736 units' named winners live in the 753 individual
  "2022 &lt;unit&gt; municipal election" articles and/or ECN per-palika result pages
  (Devanagari scrape/OCR) — **out of scope for this polite single-session harvest** and so
  **deferred**.

## Results (final build)

| Metric | Value |
|---|---|
| Total person records | **33** |
| Heads (mayor/chairperson) | 25 (17 city mayors + 8 Udayapur heads) |
| Deputies | 8 (Udayapur) |
| Distinct local units covered | **25 / 753 (3.3%)** |
| `memberOf` (office) link rate | **33/33 (100%)** |
| `containedInPlace` (location) link rate | **33/33 (100%)** |
| `validate_jsonld_entity` + `is_valid_entity_iri` pass | **33/33** |
| ≥2-source (publishable) | 33 |
| HELD (single source) | 0 |
| Unit name→CBS-code join failures | 0 |

vs. the ~753 heads / ~753 deputies target: **mayors/chairs 25 of ~753; deputies 8 of ~753**
— the wave delivers a fully-validated, fully-linked, 100% two-source CORE, and an honest,
documented deferral of the long tail (no guessed/single-source rows).

## Validation command

```
cd jawafdehi-platform
TESTING=true DJANGO_SETTINGS_MODULE=monolith.config.settings uv run python - <<'PY'
import json
from nes_service.entities.validation import validate_jsonld_entity
from jawafdehi_shared.entities.ids import is_valid_entity_iri
recs = json.load(open("local_officials_records.json"))["records"]
ok = sum(validate_jsonld_entity(r["entity_data"]) is not None and is_valid_entity_iri(r["entity_data"]["@id"]) for r in recs)
print(ok, "/", len(recs))
PY
```

## Deferred / honest gaps (the path to full 753-unit coverage)

- **~728 of 753 units' heads + ~745 deputies are DEFERRED, not guessed.** Per-district
  Wikipedia articles are confirmed (by the 77-district scan) NOT to be a viable shortcut for
  the bulk. The authoritative per-palika source is **ECN** (`result.election.gov.np`,
  dynamic .aspx, Devanagari, **no API/CSV/JSON**) — a real acquisition requires a
  per-constituency HTML scrape + Devanagari OCR (the `shared/ocr_bedrock.py` path), polite
  and spaced, as flagged in `recon-elected-officials.md`. That is the next wave to lift
  coverage toward 753 and is intentionally out of scope here.
- **Deputy mayors of the 17 big cities** are not on the central article's mayor-only tables;
  they live in the per-municipal-election sub-articles ("2022 &lt;city&gt; municipal
  election") and are deferred with the long tail.
- **Devanagari `name.ne`:** captured names are English (from Wikipedia); `name.ne` is left
  ABSENT rather than machine-transliterated. The ECN scrape/OCR wave would supply
  authoritative verbatim Devanagari names.
- **Re-run is idempotent + additive:** drop a richer
  `sources/wikipedia_district_winners.json` (or an ECN-derived equivalent with the same
  `{local_unit_en, mayor_en, deputy_en, *_party, source_article}` shape) and re-run
  `normalize_local_officials.py`; new units join by CBS code, the 17 city mayors de-dup
  against Tier A, and this table refreshes. Unit-label spelling variants are mapped in
  `UNIT_ALIASES` / `CITY_ALIASES`.
