# Entity / Party Model Harmonization Report

> **STATUS (2026-06-28): STALE paths, sound mapping — see ../../DOC-STATUS.md.** The
> NES↔Jawafdehi↔NGM representation mapping and the "harmonize on NES as canonical"
> conclusion still hold. But the file paths and the "three services" framing predate
> the monolith: the systems are now Django apps (`nes_service`, `ngm_service`, and the
> `cases`/`review` apps) in one project (`jawafdehi-platform`); the canonical join key
> is the schema.org `@id` IRI (not `entity:<prefix>/<slug>`); `JawafEntity` collapsed
> into `CaseEntityRelationship`. Read paths as the `nes-api`/`backend`/`wt-ngm-v2` era.
> Live state: `../../ARCHITECTURE.md`.

Read-only analysis of how a *person / organization / firm* is represented across
the three services, where the representations diverge, and a proposed path to
harmonize on NES as the canonical entity store.

Services compared:

| Service | File | What it stores |
|---|---|---|
| **NES** (source of truth) | `services/nes/nes_service/core/models/` | Full bilingual entity documents (Pydantic) |
| **Jawafdehi** | `services/jawafdehi/cases/models.py` | Case↔entity *binds* keyed by `nes_id` only |
| **NGM** | `services/ngm/ngm_service/courts/models.py` | Raw scraped court parties + blacklisted firms, optional `nes_id` |

---

## 1. Side-by-side field/type comparison

### 1.1 What each service stores to represent a person / org

**NES — rich canonical document.** A person/org is a Pydantic `Entity` subclass
(`Person`, `Organization` + subtypes, `Location`, `Project`).

- Identity: `slug` + `entity_prefix` (or legacy `type`/`sub_type`) → computed
  canonical `id` of the form `https://jawafdehi.org/entity/<prefix>/<slug>`
  (`entity.py:124-146`, `entity.py:211-224`).
- Names: `names: List[Name]`, each `Name` carrying `kind` (PRIMARY/ALIAS/…) and
  per-language `NameParts` for both `en` and `ne` (Devanagari), with full /
  given / middle / family / prefix / suffix decomposition
  (`base.py:108-121`, `base.py:95-106`). Plus `misspelled_names`
  (`entity.py:150-152`).
- External identifiers: `identifiers: List[ExternalIdentifier]` keyed by
  `IdentifierScheme` (wikipedia/wikidata/twitter/…/website/other)
  (`entity.py:30-53`).
- Descriptions & provenance: bilingual `short_description`/`description`
  (`LangText`), `attributions`, `contacts`, `pictures`, `tags`, free-form
  `attributes` (`entity.py:159-185`).
- Person extras: `personal_details` (birth date/place, citizenship, gender,
  address, parents/spouse, education, positions) and `electoral_details`
  (candidacies) (`person.py:75-176`).
- Org extras: `address: Address`, plus subtype fields (party symbol/chief,
  govt type, hospital beds, company reg number, contractor license/grade)
  (`organization.py:29-125`).
- Address is structured: `Address` carries a `location_id` that must itself be a
  valid `https://jawafdehi.org/entity/location/...` id (`base.py:173-201`).

**Jawafdehi — no entity attributes at all.** Jawafdehi deliberately stores
**zero** entity data. The Case↔entity relation is the `CaseEntityRelationship`
bind, which holds only the canonical `nes_id` join key
(`models.py:203-232`), plus `relationship_type` and `notes`. There is no local
entity table; display names/types are resolved from NES in-process via
`cases/services/nes_resolver.py`.

> NOTE: The task brief describes a `JawafEntity (nes_id, display_name)` model,
> but the current `cases/models.py` has **no such model** — it was removed in
> favor of the id-only bind. (`JawafEntity` survives only in the
> `mcp__jawafdehi__*_jawaf_entity` MCP tool surface.) The "Jawafdehi duplicates
> NES display_name" divergence in the brief is therefore **already resolved**
> on the case-binding path; see §3.

**NGM — raw flat scraped strings.** A party is a `CaseEntity` row:
`side` (string "plaintiff"|"defendant"), `name: CharField(500)`,
`address: CharField(500, null)`, and an optional `nes_id` (null until resolved)
(`models.py:107-129`). A blacklisted firm is a `BlacklistedFirm` row:
`firm_name`, `proprietor_name`, `address`, blacklist dates, `reason`,
`recommending_office`, optional `nes_id` (`models.py:132-156`).
`CourtCase` itself also carries denormalized `plaintiff`/`defendant`
**TextField** blobs and its own optional `nes_id` (`models.py:57-59`).

### 1.2 Field-level divergence matrix

| Concept | NES | Jawafdehi | NGM |
|---|---|---|---|
| Canonical id | `id` = `https://jawafdehi.org/entity/<prefix>/<slug>` (computed) | `nes_id` (FK-by-string) | `nes_id` (nullable, often null) |
| Name representation | `List[Name]` bilingual en/ne, parts, kind, aliases, misspellings | none (resolved) | flat `name`/`firm_name` string (single, raw scrape, language as-scraped) |
| Proprietor / sub-party | via relationships | n/a | `proprietor_name` flat string |
| Address | structured `Address` w/ `location_id` → location entity | none | flat `address` string |
| External ids | `ExternalIdentifier` + `IdentifierScheme` enum | none | none |
| Type taxonomy | `EntityType` + `EntitySubType` + `entity_prefix` registry | `RelationshipType` (role, not type) | `side` + `court_type` (role/context, not type) |
| Provenance | `ProvenanceMethod`, `attributions`, `version_summary` | `notes`, `created_at` | `scraped_at`, `extra_data` JSON |
| Lifecycle | versioned (`VersionSummary`) | none | `created_at`/`updated_at` |

---

## 2. The canonical join key reality: `nes_id`

The intended single join key across all three services is the NES canonical
entity id `https://jawafdehi.org/entity/<prefix>/<slug>` (built by
`build_entity_id_from_prefix`, validated by `validate_entity_id` against the
`ALLOWED_ENTITY_PREFIXES` registry in `entity_type_map.py:79-82`). There is **no
cross-database foreign key** — the three DBs are routed independently, so the
link is by string id only (`jawafdehi cases/models.py:209-215`).

| Service | Has `nes_id`? | Populated? | Validated? |
|---|---|---|---|
| **NES** | It *is* the id (computed `id`) | always | yes (slug + prefix registry) |
| **Jawafdehi** | yes, **mandatory** on the bind | always — a bind cannot be created without a valid id; **no display-name fallback** (`models.py:224-232`, `273-289`) | yes — `validate_nes_id` → `is_valid_entity_id` (`models.py:171-186`) |
| **NGM** | yes, but **nullable** on `CaseEntity`, `BlacklistedFirm`, and `CourtCase` | **mostly null** — populated only by an entity-resolution pass that is **not yet implemented** (see §5) | **no model-level validation** — plain `CharField(100, null=True)`, no `validate_nes_id` |

**The resolution gap.** Jawafdehi is fully resolved by construction (it can only
ever reference real NES ids). NGM is the opposite: it ingests raw scraped
strings and is *supposed* to resolve them to `nes_id` later, but that path is a
stub (§5). So today the join graph is:

```
NES entity  ◄──(always, validated)──  Jawafdehi CaseEntityRelationship.nes_id
NES entity  ◄──(rarely, unvalidated)── NGM CaseEntity.nes_id / BlacklistedFirm.nes_id
```

NGM is the weak link: its parties are predominantly **unresolved raw text**, and
even when `nes_id` is set there is no validator guaranteeing it is a
well-formed/registered id.

---

## 3. Divergences to merge (concrete friction list)

1. **NGM stores raw `name`/`firm_name` strings that should resolve to an NES
   entity.** `CaseEntity.name` (`ngm models.py:118`), `BlacklistedFirm.firm_name`
   /`proprietor_name` (`ngm models.py:137-138`), and the denormalized
   `CourtCase.plaintiff`/`defendant` TextFields (`ngm models.py:57-58`) are
   scraped Devanagari/Roman text. NES already models exactly this (bilingual
   `Name` with aliases + misspelled variants) — NGM is re-storing a lossy flat
   copy instead of resolving.

2. **Name model mismatch.** NES `Name` is bilingual, multi-part, kind-tagged,
   alias-aware (`base.py:108-121`). NGM collapses this to one `name` string in
   whatever language/spelling the scrape produced. There is no place in NGM to
   record that "बाबुराम भट्टराई" and "Baburam Bhattarai" are the same party — NES
   `misspelled_names`/`ALIAS` names solve this, but only once resolved.

3. **Address handling divergence.** NES `Address` is structured and references a
   `location` entity by id (`base.py:173-201`); NGM `address`/`BlacklistedFirm.address`
   are flat strings; Jawafdehi has none. NGM addresses are not linked to NES
   `location` entities even though the location taxonomy exists.

4. **Type vs. role taxonomies are three different vocabularies that are being
   conflated:**
   - NES classifies *what an entity is*: `EntityType`
     (person/organization/location/project) + `EntitySubType` +
     `entity_prefix` registry (`entity.py:56-111`, `entity_type_map.py:41-74`).
   - NGM `side` = "plaintiff"|"defendant" (`ngm models.py:117`) and `court_type`
     (`ngm models.py:27`) describe a party's *role in a court case* and the
     *court context* — NOT what the entity is.
   - Jawafdehi `RelationshipType` (`models.py:189-200`:
     ALLEGED/ACCUSED/RELATED/WITNESS/OPPOSITION/VICTIM/LOCATION/RESPONDENT/PETITIONER)
     describes a party's *role relative to a Jawafdehi case*.

   These are not interchangeable: NES type is intrinsic; NGM `side` and
   Jawafdehi `RelationshipType` are both *relationship/role* vocabularies that
   partially overlap but use different terms (see §4.3).

5. **Relationship-vocabulary mismatch between NGM `side` and Jawafdehi
   `RelationshipType`.** NGM has exactly 2 court-procedural roles
   (plaintiff/defendant). Jawafdehi has 9 accountability roles. They overlap
   conceptually (defendant≈ACCUSED/RESPONDENT, plaintiff≈PETITIONER/OPPOSITION)
   but neither maps cleanly onto the other, and there is no shared enum.

6. **Identifier scheme enums differ / are absent.** NES has a real
   `IdentifierScheme` enum (`entity.py:30-42`). NGM has no external-id concept;
   firm registration numbers / contractor licenses (which NES models on
   `PrivateCompany.registration_number`, `Contractor.license_number`) are not
   captured by NGM at all, even though `BlacklistedFirm` is exactly a contractor.

7. **`nes_id` field length + validation drift.** Jawafdehi `nes_id` is
   `CharField(300)` + `validate_nes_id` (`models.py:224-227`); NGM `nes_id` is
   `CharField(100)` with **no validator** (`ngm models.py:121`,`147`). 100 chars
   may truncate deep-prefix ids; the missing validator allows malformed ids.

8. **NES Relationship vocabulary is yet a fourth set.** NES has its own
   `RelationshipType` (`relationship.py:10-23`:
   AFFILIATED_WITH/EMPLOYED_BY/MEMBER_OF/PARENT_OF/…/FUNDED_BY/…) for
   *entity↔entity* graph edges — distinct from Jawafdehi's *case↔entity* roles.
   These should stay distinct (different domains) but the name collision invites
   confusion.

---

## 4. Proposed harmonization

### 4.1 NES core models are the single canonical entity definition

Keep NES `Entity`/`Person`/`Organization`/`Location`/`Project` as the **only**
place that stores entity attributes (names, identifiers, address, type,
provenance). No other service stores a name, address, or type for an entity.

### 4.2 NGM and Jawafdehi reference by `nes_id` and stop duplicating attributes

- **Jawafdehi: already compliant.** It stores only `nes_id` on the bind and
  resolves display details via `nes_resolver`. No change needed beyond wiring
  the resolver to the live NES API (it currently best-effort-reads `StoredEntity`
  or returns a stub — `cases/services/nes_resolver.py` header).
- **NGM: the gap to close.** Treat `CaseEntity.name`/`address`,
  `BlacklistedFirm.firm_name`/`proprietor_name`/`address`, and
  `CourtCase.plaintiff`/`defendant` as **raw scrape provenance only**, and make
  `nes_id` the authoritative reference once resolved. Concretely:
  - Run the entity-resolution pass (`lakehouse/medallion.py:110-126`) to populate
    `nes_id` from the bilingual NES resolution service, then have read APIs
    prefer the resolved NES entity over the raw string.
  - Add `validate_nes_id` (port Jawafdehi's validator into `jawafdehi_shared`)
    and widen NGM `nes_id` to `CharField(300)` to match Jawafdehi.
  - For blacklisted firms, resolve to `https://jawafdehi.org/entity/organization/contractor/<slug>` and
    capture the proprietor as a separate `person` entity linked by an NES
    `Relationship` (e.g. a proprietor/owner edge), rather than a flat
    `proprietor_name`.

### 4.3 Unified relationship/role vocabulary

Adopt one shared **case-party-role** enum (location: `jawafdehi_shared`) used by
both NGM-court-party projection and Jawafdehi binds, mapping the NGM
court-procedural `side` into the richer Jawafdehi set:

| Source value | Unified role | Notes |
|---|---|---|
| NGM `side=defendant` | `RESPONDENT` (court) / `ACCUSED` (substantive) | Court `side` is procedural; map to RESPONDENT for court projection. ACCUSED is a Jawafdehi editorial judgement, not derivable from `side` alone. |
| NGM `side=plaintiff` | `PETITIONER` (court) / `OPPOSITION` | In CIAA corruption cases the state/CIAA is plaintiff → PETITIONER. |
| Jawafdehi ACCUSED / ALLEGED / VICTIM / WITNESS / RELATED | unchanged | Accountability-editorial roles with no NGM equivalent — keep. |
| Jawafdehi LOCATION | unchanged | Should arguably be expressed as an NES `location` entity ref, not a role. |

**Keep distinct (document why):**
- NES entity↔entity `RelationshipType` (`relationship.py`) stays separate from
  the case-party-role enum — different domain (graph edges vs. case binding).
- NES `EntityType`/`EntitySubType` is the *intrinsic type* axis and is orthogonal
  to role; do not fold `side`/`RelationshipType` into it.
- Recommend renaming one of the two `RelationshipType` enums (e.g. NES →
  `EntityRelationshipType`, Jawafdehi → `CasePartyRole`) to end the collision.

---

## 5. Blockers to harmonization

1. **NGM entity resolution is not implemented — parties are raw scraped strings.**
   `POST /ingestion/entities/resolve` (the write-back-`nes_id`-from-NES endpoint)
   is a **501 stub** (`ngm courts/views.py:251-253` → `_IngestionStub` returns
   `HTTP_501_NOT_IMPLEMENTED`, `views.py:240-245`). The silver-layer populate
   function (`lakehouse/medallion.py:110-126`) is documented but depends on "the
   shared NES resolution service" which is not yet wired. **Until this runs,
   NGM `nes_id` is mostly null and NGM cannot be joined to NES by key** — this is
   the single biggest blocker.

2. **No bilingual fuzzy entity-resolution service exists yet.** Resolving a raw
   Devanagari/Roman court-party string to a canonical NES id requires the
   bilingual matcher referenced in `medallion.py:114` and in
   `research/entity-resolution-tech.md`; it is design-stage, not deployed.

3. **No cross-DB FK / shared search substrate.** The three DBs are routed
   independently (no FK enforcement of `nes_id`), and the shared OpenSearch plane
   that resolution would lean on is also a 501 stub (`courts/views.py:262-283`).
   Referential integrity of `nes_id` must be enforced in application code.

4. **NGM `nes_id` lacks validation and is narrower (100 vs 300 chars).** Even
   manual/partial resolution can write malformed or truncated ids; no guardrail
   exists (`ngm models.py:121,147` vs. `jawafdehi models.py:224-227`).

5. **`court_cases`/court-number identifiers are a separate join axis.** Jawafdehi
   links to court cases via `court_cases` strings like `supreme:078-WC-0123`
   (`jawafdehi models.py:460-465`); NGM keys court cases on the composite
   `(case_number, court_identifier)` (`ngm models.py:47`). Harmonizing *entities*
   does not harmonize the *case* join — that's a second, parallel key mapping to
   track but out of scope here.

---

## Summary (6 lines)

1. NES is the canonical entity store (bilingual `Name`, `entity_prefix` taxonomy, structured `Address`→location id, external identifiers, versioning); the join key is `https://jawafdehi.org/entity/<prefix>/<slug>`.
2. Jawafdehi is already harmonized: it stores only a mandatory, validated `nes_id` on `CaseEntityRelationship` and resolves display via `nes_resolver` — the brief's `JawafEntity.display_name` duplication no longer exists on the binding path.
3. NGM is the divergent service: `CaseEntity`/`BlacklistedFirm`/`CourtCase` store raw flat scraped `name`/`address` strings with a nullable, unvalidated, narrower (100-char) `nes_id`.
4. Three role/type vocabularies are conflated — NES `EntityType`/`EntitySubType` (intrinsic), NGM `side` plaintiff/defendant (court-procedural), and Jawafdehi `RelationshipType` ACCUSED/WITNESS/… (case-editorial); propose a shared case-party-role enum mapping `side`→RESPONDENT/PETITIONER while keeping editorial roles distinct.
5. Proposed fix: NGM keeps raw strings as scrape provenance only, resolves `nes_id` against NES, adds `validate_nes_id`, widens to 300 chars, and models firm proprietors as linked NES person entities.
6. Top blocker: NGM's entity-resolution write-back (`POST /ingestion/entities/resolve`) and the bilingual matcher are 501 stubs, so NGM `nes_id` is mostly null and cannot yet be joined to NES.
