# Prod migration guidance — document sources → NGM materials

_Status: GUIDANCE (rev. 2026-07-01). This is a **runbook**, not code. Companion to
the ADR [`adr-cases-own-no-documents.md`](./adr-cases-own-no-documents.md) and the
plan [`sources-into-ngm-materials-plan.md`](./sources-into-ngm-materials-plan.md)._

> **The code removal has LANDED** (branch `wt-sources-to-materials`): `DocumentSource`,
> `SourceType`, `SourceLinkRole`, `/api/sources`, `Case.evidence`, and the source
> services are gone from the NEW codebase; evidence is the `CaseMaterialReference`
> join. So the NEW deployment has no `DocumentSource` model at all — this runbook
> describes moving the DATA from the OLD deployment into the new material surface.

## Migration strategy (decided): new endpoint, one-by-one

The migration runs against the **OLD deployment** (which still has `DocumentSource`
+ `Case.evidence`). A migration endpoint/script there reads each source and POSTs
it to the NEW system's material API one at a time, then rewrites each case's
evidence to `CaseMaterialReference` rows. The NEW codebase never reads a
`DocumentSource` row directly (full removal, no shim). Source mutations (the
`DocumentSource` create/update the ingesting commands used to do) happen as part of
this migration — the ingesting commands in the new codebase raise
`NotImplementedError` until rewired to write Materials.

## What this migration does

Fold every Jawafdehi `DocumentSource` (~799 on prod, snapshot 2026-06-12) into an
NGM `Material`, and rewrite every case's evidence from the old denormalized
`Case.evidence` JSON list of `{source_id, description}` into `CaseMaterialReference`
rows keyed by `{material_iri, additional_details}`.

The projection pieces are already shipped + tested in the new codebase:
- `documentsource_to_jsonld()` + `build_source_material_iri()` (the projection),
- `CaseMaterialReference` model + `resolve_materials()` seam,
- `Material.visibility` + recompute + read-side guards,
- the material file-upload + upsert API (the write target for the one-by-one move).

So this migration is **data movement using already-tested pieces**, not new logic.

## Preconditions (verify BEFORE running)

1. Foundation slices A/B/C are deployed to the target env (migrations `cases
   0041_casematerialreference` and `materials 0004_material_visibility` applied).
2. A full DB backup / snapshot exists and restore has been rehearsed. This
   touches two databases (`default` = cases, `ngm` = materials).
3. Read-only maintenance window OR confirmed that writes to cases/sources are
   paused — the evidence rewrite is not safe against concurrent case edits.
4. Row counts captured for reconciliation:
   `DocumentSource` total, non-deleted, and count of distinct `source_id`s
   referenced across all `Case.evidence` lists.

## IRI + type mapping (already implemented — do not re-derive)

- **IRI:** `build_source_material_iri(source_id)` →
  `https://jawafdehi.org/material/jawafdehi/<ident>`, where `<ident>` is the
  `source_id` with the `source:` prefix dropped and `:` → `.`, lowercased
  (`source:20240115:ab12cd` → `.../jawafdehi/20240115.ab12cd`).
- **material_type:** `material_type_for_source_type(source_type)` — CIAA/AG →
  `charge_sheet`, OAG → `official_report`, COURT_ORDER → `court_order`, LAW_OR_BILL
  → `legal_corpus`, NEWS → `news`, SOCIAL_MEDIA → `social_media`, else `document`.
- **JSON-LD body:** `documentsource_to_jsonld(...)` — carries `name` (title),
  `description`, `associatedMedia` (the roled `{link, role}` list, all five roles
  preserved incl. MARKDOWN/SOURCE_PAGE), `about` (related_entities NES IRIs),
  `datePublished` (publication_date), `jawafdehi:sourceType` (audit of origin).

## Migration steps (ordered)

### Step 1 — Backfill Materials from DocumentSources
For each `DocumentSource` (INCLUDING `is_deleted=True` — a retracted source may
still be cited by a historical case; migrate it, then let visibility/soft-delete
gate it):
- Build `(doc, material_type)` via `documentsource_to_jsonld(...)` from the row's
  fields.
- Upsert the Material with a DIRECT `Material.from_jsonld(doc, material_type=...)`
  + `full_clean()` + `save()`. **Do NOT route through `bulk_ingest`** — its ≥2
  distinct-publisher HOLD gate would hold every single-source case document (plan
  C1). This is the single-source exemption path.
- Carry `DocumentSource.is_deleted` → `Material.is_deleted` so a retracted source
  becomes a soft-deleted Material.
- Idempotent: re-running upserts the same `@id`; safe to resume after a failure.

### Step 2 — Rewrite case evidence → CaseMaterialReference
For each `Case`, for each entry in `Case.evidence` (ordered):
- Resolve the entry's `source_id` → `material_iri` via
  `build_source_material_iri(source_id)`. (Handle the legacy embedded-dict shape
  `{"source_id": {...}}` the serializer's `resolve_source_id` already tolerates.)
- Create a `CaseMaterialReference(case=, material_iri=, additional_details=<the
  entry's old "description">, ordinal=<list index>)`.
- Skip/​log entries whose `source_id` has no matching DocumentSource (dangling
  reference) rather than aborting the whole case.
- `additional_details` is OPTIONAL — an empty/missing old description → `""`.

### Step 3 — Initialize visibility
Run `materials.visibility.recompute_all()`. This sets every case-referenced
Material's visibility from its referring cases' states (PUBLISHED→LISTED,
IN_REVIEW→UNLISTED, DRAFT/CLOSED→PRIVATE). NGM-native materials keep default
LISTED. **This step is what prevents a draft case's evidence from leaking** — do
not skip it.

### Step 4 — Reindex + rebuild discovery
- Trigger a unified-search reindex of materials (or rely on the per-save signal
  if Step 1 saved through the ORM — confirm the index reflects the new
  materials + honors visibility: non-LISTED must be absent).
- Regenerate Sitemaps/ResourceSync and confirm only LISTED materials appear.

## Verification (must all pass before code removal)

1. **Counts:** every non-`is_deleted` DocumentSource has a corresponding live
   Material at its expected IRI; deleted sources → soft-deleted Materials.
2. **Evidence parity:** for a sample of cases across DRAFT/IN_REVIEW/PUBLISHED,
   the set of `CaseMaterialReference.material_iri` equals the set of
   `build_source_material_iri(source_id)` over the old `Case.evidence`, and
   ordering (`ordinal`) matches.
3. **Draft-leak check (critical):** pick a DRAFT-only-referenced source; confirm
   its Material is PRIVATE, returns 404 to an anonymous
   `GET /api/materials/?iri=...`, is absent from anon search, and is absent from
   the sitemap. Confirm an authed caseworker CAN retrieve it.
4. **Published parity:** a published case's evidence materials are LISTED,
   publicly retrievable, searchable, and in the sitemap.
5. **Round-trip render:** the case detail page (evidence cards) renders the same
   documents via `resolve_materials()` as it did via DocumentSource.

## Rollback

Steps 1–2 are additive (new Material rows + new CaseMaterialReference rows) and do
NOT mutate `DocumentSource` or `Case.evidence` — so rollback is: delete the
created Materials (those with source `jawafdehi`) and the CaseMaterialReference
rows, and restore visibility if `recompute_all` altered NGM-native rows (it should
not — they have no case referrers). The old read path still works because
`DocumentSource`/`Case.evidence` are untouched until the separate removal slice.

## Code-removal slice — DONE (2026-07-01)

The backend removal already landed on `wt-sources-to-materials`:
- ✅ `CaseDetailSerializer.get_evidence` + `review/case_provider` + `review/jds_client`
  read `CaseMaterialReference` + `resolve_materials`; the review source dict carries
  `material_type` (the ~6 token reads updated — ADR D-F).
- ✅ `DocumentSource`, `SourceType`, `SourceLinkRole`, `DocumentSourceViewSet` +
  serializers, `/api/sources`, `Case.evidence`, and the `cases/services` source_*
  helpers are removed; drop migration `cases/0042` drops the model + column
  (ADR D-B / D-G).
- ✅ Data-ingesting commands (CIAA enrichers, `map_press_release_files`,
  `seed_allegations`, `seed_jawafdehi`, `case_importer`, `ciaa_draft_case_service`)
  raise `NotImplementedError` until rewired to write Materials.

- ✅ Evidence WRITE paths wired: `POST /api/cases` + `PATCH /api/cases/{id}` (`/evidence`
  ops) create/replace `CaseMaterialReference` rows; visibility recompute fires live on
  create / evidence-change / state-transition / soft-delete over current+removed material
  IRIs (the ADR draft-leak guard is now live, not just the `recompute_all` backstop).

**Still pending (separate follow-ups):**
- Rewire the stubbed ingesting commands to create `Material` + `CaseMaterialReference`.
- The actual prod DATA migration (this runbook). Run `recompute_all()` once post-migration
  to initialize visibility for all backfilled materials.
- Frontend + MCP "Document Source" terminology purge (ADR D-E).
