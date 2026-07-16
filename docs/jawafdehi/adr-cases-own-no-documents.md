# ADR — Cases own no entities and no documents; both link out by required IRI

_Status: ACCEPTED (2026-07-01). Amends three prior docs — see §6. Owner-decided in
session._

## Context

The platform already treats **NES entities** as the sole owner of "things": a
Jawafdehi case does **not** carry its own entity table — it binds to NES entities
through `CaseEntityRelationship`, whose `nes_id` is a **required, strictly-validated
canonical `@id` IRI** (`cases/models.py:209-304`, `validate_nes_id`). Display data is
resolved from NES at read time (`cases/services/nes_resolver.py`).

Documents were **not** yet symmetric. Jawafdehi still owns a full `DocumentSource`
model (`cases/models.py:755-931`) plus a denormalized `Case.evidence` JSON list of
`{source_id, description}`. The prior plan
[`sources-into-ngm-materials-plan.md`](./sources-into-ngm-materials-plan.md)
(2026-06-28) moved the document *content* into NGM Materials but **kept a thin
`DocumentSource` reference row** (retaining `contributors`, soft-delete, the
`material_iri` bind).

This ADR completes the symmetry: **`cases` owns no documents either.** It supersedes
the "thin row" step of that plan.

## Decision

**Principle: `cases` owns only IRI-keyed join rows. Every join carries a required,
strictly-validated canonical IRI into NES (entities) or NGM (materials); no local
copy of the referenced thing, and — per the monolith cross-DB rule — a string IRI
reference, never a cross-DB FK.**

### D-A · Two symmetric joins

| Join (in `default` DB) | → target (DB) | Required key | Local columns |
|---|---|---|---|
| `CaseEntityRelationship` *(exists, unchanged)* | NES entity (`nes`) | `nes_id` | `relationship_type`, `notes` |
| **`CaseMaterialReference`** *(new)* | NGM material (`ngm`) | **`material_iri`** (`validate_material_iri`, strict) | **`additional_details`** (optional), ordinal |

- Replaces `Case.evidence` JSON. Unique on `(case, material_iri)`; FK to `Case`
  (CASCADE); `created_at`. Mirrors `CaseEntityRelationship` field-for-field.
- The per-case evidence note is renamed `description` → **`additional_details`** and
  is **optional**. The Material's own JSON-LD `description` stays global and
  separate (a case explains *why this doc matters to this case*; the Material
  describes itself).

### D-B · `DocumentSource` is removed entirely (supersedes the plan's Phase 2 thin row)

The model, `/api/sources`, `DocumentSourceViewSet`, the `Case.evidence` JSON, and the
bespoke source-upload path all retire. There is no Jawafdehi-side document row of any
kind. The document resource **is** `/api/materials`. The "document source" *concept and
vocabulary* — not just the model — is banished from the backend too (see D-G).

### D-C · Per-object `contributors` ACL is dropped → NGM write role (supersedes the plan)

Source-level access was a per-object `contributors` M2M on `DocumentSource`. With the
row gone, material writes are gated **only** by the NGM/materials write role. No
per-material ownership ACL is carried forward.

### D-D · Materials = universal document store; ONE `material_type` vocabulary end-to-end

Every source becomes a full Material with an `@id`. Reuse the plan's finalized IRI
scheme: source segment **`jawafdehi`**, ident = normalized `source_id`
(`source:YYYYMMDD:hex` → `YYYYMMDD.hex`; see plan C3). Issuer distinction lives in
`material_type`, **not** the IRI prefix.

**Refinement of the plan's §4 map:** do **not** collapse NEWS/SOCIAL/MISC into the
single generic `DOCUMENT` token — the frontend tiers evidence Primary/Legal/**Secondary**
and badges by type, so it needs signal. Extend `MaterialType`
(`materials/jsonld.py:56-85`) with **`news`** and **`social_media`** (Secondary tier);
keep the governance mappings from the plan's §4 (`charge_sheet`, `court_order`,
`legal_corpus`, `official_report`) and route MISC + COURT_FILING_OTHER → existing
`document`. This **one** vocabulary is the single source of truth: backend enum →
frontend `MATERIAL_TYPES` → tier grouping → badges. The current frontend
`DocumentSourceType` taxonomy (a *different*, drifted set) is deleted, not mapped.

**linkRole vocabulary:** the control-plane design §3.1 listed only
`RAW`/`ALTERNATE`/`PERMALINK`. Add **`MARKDOWN`** and **`SOURCE_PAGE`** so no existing
`SourceLinkRole` value is lost (all five appear in prod — plan §4a).

### D-E · Frontend: "Document Source" terminology is banished

Not just the API client — the concept. In `jawafdehi-frontend`:
- Retire the source CRUD (`pages/admin/jawafdehi/AdminSources.tsx`,
  `AdminSourceForm.tsx`, `admin-api.ts` `listSources/getSource/create/update/delete`).
  Its friendlier fields (title, type dropdown, roled links, file upload) are **ported
  onto** the NGM material admin (`NgmMaterials.tsx`/`NgmMaterialForm.tsx`), replacing
  the raw JSON-LD editor for these.
- Public `CaseDetail.tsx` resolves evidence via `getMaterial(material_iri)`; the
  evidence card reads Material JSON-LD (reuse `MaterialProfile.tsx`'s `associatedMedia`
  extraction) and re-tiers on `material_type`. `DocumentSourceCard` →
  `EvidenceMaterialCard`.
- Evidence editing (`AdminCaseForm.tsx`): `{source_id, description}` →
  `{material_iri, additional_details}`.
- LLM guest adapter (`guest-chat-adapter.ts` `resolveCaseSources`) → material_iri.
- Types/constants: `DocumentSource`, `SourceLink`, `EvidenceEntry`,
  `SourceLinkRole`, `DocumentSourceType` retire → `Material` + unified
  `MATERIAL_TYPES`. i18n keys (`documentSource.*`, `DocumentSourceTypeKeys`) renamed.

### D-G · Backend: the "document source" concept is banished too (symmetric to D-E)

Removal is not just dropping the model — the *vocabulary* goes, so no future reader
mistakes a Material for a distinct "source". In `jawafdehi-api`, retire/rename:
- **Models/enums** (`cases/models.py`): `DocumentSource` (removed), `SourceType`
  (removed — folds into `MaterialType`, D-D), `SourceLinkRole` (renamed/moved to the
  Material link-role vocab — it already mirrors `materials/jsonld._ROLE_ENCODING_HINTS`).
- **API** (`cases/urls.py:26`, `cases/serializers.py`, `cases/api_views.py`):
  `DocumentSourceViewSet`, `DocumentSource{Serializer,CreateSerializer,UpdateSerializer}`,
  `SourceLinkField`, the `sources` route — all removed. `/api/materials` is the resource.
- **Evidence field** (`cases/fields.py`, `cases/serializers.py`): `EvidenceListField`'s
  `{source_id, description}` → the `CaseMaterialReference` join (D-A); the
  `resolve_source_id`/`get_evidence` enrichment reads Materials.
- **Services** (`cases/services/`): `source_files.py`, `source_markdown.py`,
  `source_classifier.py`, `storage_links.py` — fold into the Material upload/JSON-LD
  path (the R6 endpoint already subsumes upload→R2→MediaObject); rename away from
  "source".
- **MCP** (`jawafdehi-mcp`, worktree `wt-mcp-r4`): `UploadDocumentSourceTool`
  (`tool = "upload_document_source"`, `POST /api/sources/`) → a material-upload tool
  hitting `POST /api/materials/{source}/{ident}/file`; the `/api/sources/{id}/` reads
  in `jawafdehi_cases.py:237` → material IRI resolution. (Rolls into the open R4 MCP work.)

**NOT banished — keep:** the token **`source`** in the material IRI
`/material/<source>/<ident>` (`ids.py:141-145`) and the `Material.source` column mean
*issuer/collection* (court/ciaa/ag/…), a different concept from the Jawafdehi
"document source". That vocabulary stays.

### D-F · Review engine rewires to the Material vocabulary

The review engine never touches `DocumentSource` directly — it consumes a nested
`source` dict `{title, source_type, url[]}` assembled by
`CaseDetailSerializer.get_evidence()` / `review/case_provider.py`. Post-collapse that
dict is assembled by a **`resolve_materials(iris)`** service (mirroring
`resolve_entities`) and carries `material_type` + `linkRole`. The ~6 token reads
(`review/rules_engine.py:42,179-180`, `casetype.py:44`, `scorer.py:139,328`,
`bedrock_judge.py:186`, `jds_client.py:116`) switch `source_type` → `material_type`
and to the new token values. This is a contained rewrite of the assembly seam + those
reads, not surgery across the 23 rules.

## Inherited, now-mandatory: Material `visibility` (plan C2 / OQ4)

Removing `DocumentSource` deletes the only Jawafdehi-side row that could have gated a
draft-only source. Every source-Material now lives naked in the `ngm` DB, where
`corpus._iter_materials()`, unified search, and the retrieve endpoint treat Materials
as public. **The plan's `visibility` field (LISTED / UNLISTED / PRIVATE, default
LISTED) is therefore not optional under this ADR — it is required**, and its recompute
hook moves onto the `CaseMaterialReference` add/remove path **in addition to** case
state transitions (plan OQ5's "single chokepoint" concern now includes join-row
edits). A periodic reconciler backstop is recommended. Without this, a draft case's
evidence leaks into public search/sitemaps.

### Amendment: `visibility` is derived from a caseworker `visibility_policy`

The pure "MAX over referring case states" rule above is correct for case-uploaded
evidence but wrong for an already-public document: once the doc-dedup work
re-points a case's evidence from a duplicate upload onto a canonical corpus doc
(court order, CIAA press release, charge sheet), a DRAFT referrer would demote
that public document to PRIVATE. So `visibility` is now the DERIVED, cached result
of a caseworker-controlled `visibility_policy` (`materials.models.Policy`):
`PUBLIC` → always LISTED; `PRIVATE` → always PRIVATE; `CASE_GATED` → the MAX over
referring case states (the historical rule). A material is born
`default_policy_for(source)` — corpus (`source != jawafdehi`) → `PUBLIC`,
case-upload (`source == jawafdehi`) → `CASE_GATED` — INSERT-only, so re-sourcing
never clobbers a manual policy. Caseworkers flip a material's policy via
`PATCH /api/materials/`. The recompute path (`materials.visibility`) and its
trigger sites are unchanged; only the state→visibility map gained the policy
short-circuit. `recompute_all()` heals the corpus documents the old rule
mis-demoted.

## Migration (design-only for now — owner-decided)

The ~799 existing `DocumentSource` rows + all evidence refs must be backfilled to
Materials and `{source_id}` → `{material_iri}`, then the model dropped. The mechanics
(one-shot hard-cut vs. dual-write grace period) are **deferred to implementation**;
this ADR settles only the target model.

## §6 · Documents amended by this ADR

- [`sources-into-ngm-materials-plan.md`](./sources-into-ngm-materials-plan.md) —
  Phase 2 "thin `DocumentSource` row" is **superseded** by D-A/D-B (full removal +
  `CaseMaterialReference`); `contributors` retention **superseded** by D-C; §4 type
  map **refined** by D-D (add `news`/`social_media`, don't collapse to `DOCUMENT`).
  Phase 0/0b/1/3 and the `visibility` design **stand** (visibility now mandatory).
- [`control-plane-api-design.md`](../control-plane-api-design.md) — §1's `/api/sources`
  as a distinct Jawafdehi resource is removed (D-B); §3.1 linkRoles extended (D-D);
  D5 (firm blacklist) unaffected.
- [`unified-api-refactor-plan.md`](../unified-api-refactor-plan.md) — R5a "DocumentSource
  CRUD" and R6 separate upload path **invert** to *retire sources into materials*
  (D-B, D-E); the `/api/sources` public-API row is removed.
