# Plan — Onboard Jawafdehi case sources into NGM as Materials


_Status: PROPOSED (2026-06-28). Planning artifact; not yet built. Owner decision:
**NGM is the data lake and owns all material storage.** Jawafdehi case
"sources" (laws, court orders, CIAA press releases, news links, photos, …)
become first-class NGM `Material` documents; Jawafdehi keeps only a reference._

## 1. Problem / current state

Today there are **two unrelated things both called "document sources"**, and the
Jawafdehi one is **not onboarded into NGM at all**:

| | Jawafdehi `DocumentSource` (case evidence) | NGM `document_sources` (a JSONB column) |
|---|---|---|
| Where | `services/jawafdehi/cases/models.py:755`, `default` DB | a field on `court_cases` rows, `ngm` DB |
| What | evidence referenced by cases; `{link, role}` URLs, `source_type`, `related_entities`, `publication_date` | roled links that ride along a court-case record |
| In NGM Materials / lake? | **No** | Yes — flattened to `associatedMedia` by `jsonld.py:141` |
| Has an `@id` IRI? | **No** — just `source_id` (`source:YYYYMMDD:hex8`) | n/a (rides on the case) |
| In unified search? | **No** (the `jawafdehi-cases` doc indexes case text only — `cases/search_index.py:53` — not even source titles) | via the court-case Material |
| In discovery / Sitemaps? | **No** (`monolith/discovery/corpus.py` enumerates only entity/material/courtcase/case) | via the court-case Material |

So a large share of case evidence is exactly the *kind* of governance document
NGM Materials represent (CreativeWork with roled media links), but it lives only
in the Jawafdehi DB + the gated `GET /api/sources/` API — outside the lake,
search, discovery, and the `@id` linked-data surface.

**The link model already matches.** The Jawafdehi `url` `{link, role}` shape is
byte-for-byte what `media_objects_from_document_sources` (`jsonld.py:141`) already
consumes for court-case Materials. The JSON-LD projection is mostly reuse.

## 2. Target state (decisions locked with owner)

1. **NGM `Material` is the system of record** for the document. Jawafdehi keeps
   only the **`@id` IRI reference** + case-specific linkage (contributors, which
   evidence/case references it). Storage of the document content moves to NGM.
2. **Every source becomes a full Material with an `@id`** (no "thin" tier — see
   §4a: all prod sources carry a RAW link, so there's no metadata-only source).
   The only real split is in *blob custody* (decision #3): a RAW link pointing at
   our storage relocates into NGM; a RAW link that's an external URL (a news
   article, a social post) stays as-is.
3. **Blob custody moves into NGM's lakehouse/R2.** Uploaded files/photos relocate
   from the Jawafdehi `case_uploads/` prefix into NGM-owned R2 (bronze tier), so
   the lake holds the bytes, not just metadata.
4. **`Case.court_cases` is refactored to NGM court-case IRIs.** The field today
   (`models.py:477`) stores `{court}:{case_number}` strings (e.g.
   `supreme:078-WC-0123`). It is refactored to store canonical courtcase `@id`
   IRIs (`https://jawafdehi.org/courtcase/<court>/<case_number>`) — the platform
   already has `build_courtcase_iri`/`parse_courtcase_iri` (`ids.py:278-340`). So
   a case references its **court cases** by NGM IRI and its **documents** by
   Material IRI: both case linkages become `@id` references into NGM.

### Visibility model — three tiers, derived from referrers (owner-decided)

Because NGM is the SoR, a source Material **always exists** (even a draft-only
source's content lives in NGM). Visibility is a **derived** property recomputed
as the **max over all cases that reference the source** ("unlisted" =
YouTube-unlisted semantics — reachable by direct link, not indexed):

| Material tier | Condition (max across referrers) | Retrieve by IRI | Unified search | Sitemaps/ResourceSync |
|---|---|---|---|---|
| **LISTED** | ≥1 **PUBLISHED** case references it | ✓ public | ✓ | ✓ |
| **UNLISTED** | ≥1 **IN_REVIEW** ref, none published | ✓ public (direct link) | ✗ | ✗ |
| **PRIVATE** | only DRAFT refs (or none) | ✗ public; authed caseworker/readonly only | ✗ | ✗ |

Multi-referrer resolution is **max-visibility**: a source referenced by A
(published) + B (draft) is **LISTED** (A wins); A (in-review) + B (draft) is
**UNLISTED**. Publishing A is never blocked by B being a draft, and B being
in-review never makes the source publicly *searchable* — only directly
reachable. This requires a real `visibility` field on `Material` (not
create/retract), which also generalizes cleanly: NGM court-case Materials are
simply always `LISTED`.

Once a `Material` exists **and is LISTED**, **search and discovery are free**: `Material.post_save`
auto-indexes into `ngm-materials` (`materials/signals.py:17`) and
`corpus._iter_materials()` (`corpus.py:97`) auto-enumerates it for
ResourceSync + Sitemaps. The work is in *creating the right Material* and
*rewiring Jawafdehi to reference it*.

## 3. Hard constraints discovered (these shape the design)

- **C1 — Do NOT use `bulk_ingest`.** `materials/bulk_ingest.py:177` enforces a
  **≥2 distinct-publisher** HOLD gate (correct for NES-style sourced entities). A
  single case document is **one** publisher — it would always be HELD. Case
  evidence needs a **direct Material upsert** that bypasses the publisher-count
  gate. (The document's authority comes from the case, not from N corroborating
  publishers.)
- **C2 — Visibility is a derived `Material` field, not existence.** NGM Materials
  are treated as **all public** today: `corpus._iter_materials()` yields *every*
  Material into Sitemaps, search has no visibility filter, and `Material` has no
  visibility column. Since NGM is now the SoR, the source Material must **exist
  even for a draft-only source** — so visibility cannot be modeled as existence.
  Add a **`visibility` field** (LISTED / UNLISTED / PRIVATE) recomputed as the
  **max across referencing cases' states** (see the three-tier table in §2). Every
  Material consumer must honor it:
  - `corpus._iter_materials()` (`corpus.py:97`) → filter to **LISTED** only.
  - unified search → index LISTED; show UNLISTED/PRIVATE only to authed
    caseworker/readonly (mirrors "draft/in-review not indexed" rule); never to anon.
  - the Material JSON-LD retrieve endpoint → serve LISTED + UNLISTED to the public
    (UNLISTED = reachable by direct IRI), 404 PRIVATE for anon.
  This is the riskiest part of the design: every public surface must filter, or a
  draft source leaks. (Court-case Materials are always LISTED — no behavior change
  for them once the field defaults correctly.)
- **C3 — IRI normalization.** Material IRI grammar (`ids.py:143-145`): source
  `[a-z0-9_]+(/…){0,3}`, ident `[a-z0-9][a-z0-9._-]*`. The `source_id`
  `source:20240115:ab12cd` has **colons** → invalid ident. Normalize to
  `@id = https://jawafdehi.org/material/jawafdehi/20240115.ab12cd` (source
  segment `jawafdehi`; ident = drop the `source:` prefix, `:`→`.`). Stable +
  reconstructable from the row.
- **C4 — Cross-DB.** `Material` lives in the `ngm` DB, `DocumentSource` in
  `default`. **No cross-DB FK** — Jawafdehi stores the IRI **string** and calls the
  NGM app **in-process** (not REST) to upsert/delete, per the monolith rules.
- **C5 — Bronze is stubbed.** `lakehouse/medallion.py:46` `ingest_raw()` raises
  `NotImplementedError`. Moving blob bytes into the lake (decision #3) **requires
  implementing bronze ingest first** — so blob custody is the heaviest sub-phase
  and is sequenced last.
- **C6 — Naming-collision / dedup.** A CIAA charge sheet may exist both as case
  evidence here **and** as a court-case `document_sources` link in NGM. Whether
  the two converge on one Material IRI is an **open question** (§6).

## 4. Type mapping (Jawafdehi `SourceType` → NGM `MaterialType`)

**OQ2 RESOLVED — confirmed against the live `SourceType` enum (`models.py:336`)
and a prod sample (799 sources, snapshot 2026-06-12).** The 9 real types + their
prod frequency, mapped to NGM `MaterialType`. The Jawafdehi values line up almost
1:1 with the existing `INDEX_SOURCE_TYPE_TO_MATERIAL` keys (`jsonld.py:266`) — add
a `JAWAF_SOURCE_TYPE_TO_MATERIAL` map (or reuse that one; the keys match):

| Jawafdehi `SourceType` | prod n (%) | NGM `MaterialType` | schema.org `@type` |
|---|---:|---|---|
| `CIAA_PRESS_RELEASE` | 225 (28%) | `CHARGE_SHEET` | DigitalDocument + `jawafdehi:ChargeSheet` |
| `COURT_ORDER` | 214 (27%) | `COURT_ORDER` | Manuscript/DigitalDocument |
| `NEWS` | 210 (26%) | `DOCUMENT` | DigitalDocument |
| `MISC` | 76 (10%) | `DOCUMENT` | DigitalDocument |
| `AG_ABHIYOG_PATRA` | 39 (5%) | `CHARGE_SHEET` | DigitalDocument + `jawafdehi:ChargeSheet` |
| `LAW_OR_BILL` | 13 (2%) | `LEGAL_CORPUS` | Legislation |
| `SOCIAL_MEDIA` | 10 (1%) | `DOCUMENT` | DigitalDocument |
| `COURT_FILING_OTHER` | 10 (1%) | `DOCUMENT` | DigitalDocument |
| `OAG_AUDIT_REPORT` | 2 (<1%) | `OFFICIAL_REPORT` | Report |

~82% are governance documents (CIAA/court/AG/law/OAG) — squarely NGM Material
territory. Note `INDEX_SOURCE_TYPE_TO_MATERIAL` already maps these exact keys, so
the map is largely free.

### 4a. Prod-sample facts that correct earlier assumptions
- **Every source has a RAW link (799/799)** — a role-gate requires ≥1 RAW per
  source. So the "thin / external-only" framing in decision #2 is **wrong as
  stated**: the distinction is not RAW-vs-none, it's **uploaded-blob RAW** (file
  in our R2/S3) vs **external-URL RAW** (e.g. a news article's own URL). This
  retargets Phase 3: relocate only RAW links pointing at *our* storage; leave
  external-URL RAW links in place. Every source still becomes a full Material with
  an `@id` (no "thin" tier needed).
- **All five roles appear** (RAW 799, ALTERNATE 203, SOURCE_PAGE 195,
  PERMALINK 100, MARKDOWN 57) — the shaper must handle all; it already does via
  `media_objects_from_document_sources` (`jsonld.py:141`).
- **`publication_date` present on only ~37%** (232/799) → `datePublished` is
  frequently absent; the shaper must keep it optional (it does).

## 5. Phased plan

### Phase 0 — Contracts (no behavior change)
- Add `build_source_material_iri(source_id)` + ident-normalization to
  `shared/jawafdehi_shared/entities/ids.py` (source segment `jawafdehi`).
- Add `JAWAF_SOURCE_TYPE_TO_MATERIAL` (§4) to `materials/jsonld.py`.
- Write `documentsource_to_jsonld(source)` in `materials/jsonld.py`: builds the
  JSON-LD doc — `@id` (C3), `@type`/`additionalType` from the type map, `name`
  from `title`, `description`, `datePublished` from `publication_date`,
  **`associatedMedia` reusing `media_objects_from_document_sources`** on the
  `{link, role}` `url` list, and **`about`** = the `related_entities` IRIs.
  Validate with `validate_material_jsonld` (`jsonld.py:408`).
- Unit-test the shaper (pure, no DB) — mirrors the existing
  `court_case_to_jsonld` tests.

### Phase 0b — Refactor `Case.court_cases` to courtcase IRIs (decision #4)
Independent of the source work; can land first since it's self-contained.
- Swap `validate_court_cases` (`models.py:480`) to accept/validate canonical
  courtcase `@id` IRIs via `is_valid_courtcase_iri` (`ids.py:295`).
- Data migration: each `{court}:{case_number}` → `build_courtcase_iri(court,
  case_number)`. Watch the case-number normalization the existing
  `normalize_case_number` applies (Devanagari digits, zero-padding) so the IRI's
  `<case_number>` segment matches the NGM courtcase IRI exactly (else the join
  key won't line up).
- Update the CR-number extraction in `Case.save()` (`models.py:549-559`, which
  parses `court_cases`) and any serializer/API/MCP surface that emits or accepts
  the `court:case` string form.
- Verify against the courtcase IRI grammar `<court>` = `[a-z0-9]+`, so `special`,
  `supreme`, district court ids all fit; flag any court id with hyphens/uppercase.

### Phase 1 — Projection: source → Material + visibility field (NGM = SoR)
- **Add `visibility` to `Material`** (LISTED / UNLISTED / PRIVATE; default LISTED
  so court-case Materials are unaffected). Migration in the `ngm` DB. Index it.
- Make consumers honor it (C2): `corpus._iter_materials()` → LISTED only; unified
  search → LISTED for anon, +UNLISTED/PRIVATE for authed caseworker/readonly;
  Material retrieve endpoint → LISTED+UNLISTED public, PRIVATE authed-only.
- Add an in-process service (C4): `upsert_source_material(source, visibility)` and
  `recompute_source_visibility(source_id)` in the NGM materials app — a **direct
  `Material` upsert that bypasses the ≥2 gate** (C1): `Material.from_jsonld(...)` +
  `full_clean()` + save (the signal re-indexes it). The Material **persists**
  across state changes; only its `visibility` flips.
- **Drive visibility off case state transitions, not source save.** On any case
  publish / send-to-review / unpublish / delete, recompute the visibility of every
  source that case references = **max over all cases still referencing it**
  (PUBLISHED→LISTED, IN_REVIEW→UNLISTED, else PRIVATE). Reuse the case
  state-transition path (where the `@id` IRI is minted at PUBLISH) — this is the
  exact hook that already knows the referrer set, and it correctly handles the
  shared-source case (A published + B draft → LISTED).
- Add a `backfill_source_materials` management command: create every source's
  Material and set initial visibility from the current referrer states.
- Result after Phase 1: every source is a Material with an `@id`, correctly
  listed/unlisted/private. **Storage is still duplicated** (Jawafdehi row +
  Material doc) — acceptable interim; Phase 2 removes the duplication.

### Phase 2 — Jawafdehi becomes reference-only (collapse the duplication)
- Add `material_iri` to `DocumentSource` (or to the case `evidence` entry). Decide
  the **thin-row shape**: `DocumentSource` retains only case-linkage fields
  (`contributors`, soft-delete, the `material_iri` bind) and **stops being the
  store** for `title`/`url`/`source_type`/`publication_date`/`related_entities` —
  those resolve from the Material.
- Repoint `GET /api/sources/` (and retrieve/serializers, `cases/api_views.py:655`)
  to **resolve display data from the NGM Material** (in-process) + the Jawafdehi
  linkage, keeping the existing response contract and the published/in-review
  visibility gate.
- Data migration: for each existing source, ensure its Material exists (Phase 1
  backfill) then null/drop the now-derived columns. Keep `source_id` as the
  human-facing handle (maps 1:1 to the IRI).
- Update create/update endpoints + `upload_document_source` MCP tool to write
  through to the Material as SoR.

### Phase 3 — Blob custody into the NGM lakehouse (heaviest; depends on C5)
- Implement `lakehouse/medallion.ingest_raw()` (currently stubbed) to write bytes
  to `NGM_BRONZE_BUCKET` (R2) with provenance — reuse the boto3 client recipe in
  `lakehouse/index_publish._make_s3_client()` (`index_publish.py:124`).
- Rewire the upload path: `cases/services/source_files.store_file_as_link()` +
  `cases/storage.HashedFilenameS3Boto3Storage` → write into NGM bronze instead of
  the Jawafdehi `case_uploads/` prefix; the returned RAW link points at the
  NGM-owned object.
- One-time migration of existing `case_uploads/` blobs into NGM bronze; update the
  RAW links on affected Materials.
- **Distinguish blob-RAW from external-RAW (§4a):** only relocate RAW links whose
  host is our storage (R2/S3 `case_uploads/` / configured domain); RAW links that
  are external URLs (news, social) are left in place. A simple host check on each
  RAW link decides. (Roughly the NEWS/SOCIAL_MEDIA slice + any external-linked
  governance doc — confirm by host, not by `source_type`.)

## 6. Open questions

- **OQ1 (dedup, C6):** Should a court order that is *both* case evidence and an
  NGM court-case attachment share **one** Material IRI? Options: (a) keep separate
  IRIs (`/material/jawafdehi/…` vs `/material/court/…`) and link via `sameAs`;
  (b) converge on the court IRI when a court ref is known. Affects dedup + the
  ≥2-source story.
- **OQ2 — RESOLVED.** 9 live `SourceType` values confirmed + prod frequencies;
  mapping finalized in §4. (No standalone "photo" type exists — images ride inside
  a source's links, not as their own `SourceType`; the earlier "photo" row is
  dropped.)
- **OQ4 — RESOLVED.** `Material` gains a derived three-tier `visibility` field
  (LISTED/UNLISTED/PRIVATE); see §2 + C2 + Phase 1. (Chosen over create/retract
  because NGM-as-SoR requires the Material to exist for draft-only sources, and
  the owner wants in-review sources reachable-but-unlisted.)
- **OQ5:** Recompute trigger robustness — case state transitions are the hook, but
  confirm there's a single chokepoint for *all* transitions (publish, unpublish,
  review, soft-delete, hard-delete, evidence-list edit that adds/removes a
  source_id). A missed path = stale visibility (either a leak or an orphaned
  PRIVATE). Consider a periodic `recompute_all_source_visibility` reconciler as a
  backstop, mirroring the casework reaper pattern.

## 7. Why this is mostly reuse (effort signal)

- JSON-LD shaping: reuses `media_objects_from_document_sources` + the
  `type_for`/`MATERIAL_TYPES` machinery; new code ≈ one shaper fn + one type map.
- Search + discovery: **zero new code** — signal + corpus already cover any
  Material.
- Real new work concentrates in: the `visibility` field + recompute-on-transition
  bookkeeping (C2/OQ5) and making every Material consumer honor it; the
  `court_cases`→IRI refactor + migration (Phase 0b); the Jawafdehi ref-only
  refactor + data migration (Phase 2); and implementing bronze ingest + blob
  migration (Phase 3, C5).
