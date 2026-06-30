> **STATUS (2026-06-28): SUPERSEDED — the platform did a FULL remodel, NOT the
> serialization-layer approach this doc recommends.** This doc proposes a read-time
> `to_jsonld()` mapper that leaves the Pydantic store and the `entity:<prefix>/<slug>`
> id contract untouched, exposed behind `?format=jsonld`. The OPPOSITE shipped (commit
> `3da5f14`; see `../../ARCHITECTURE.md`
> §2; live `services/nes/nes_service/entities/models.py`): schema.org JSON-LD became the
> canonical **stored** form, keyed by the `@id` **IRI** — the per-type Pydantic models
> were **deleted**, the `entity:` scheme is **gone**, and there is no `?format=jsonld`
> serialization layer. So §1's "serialization layer, not a remodel" decision, the
> `entity:`-id-"must-not-change" contract (§1, §6), and the "rejected alternative —
> remodeling" framing (§1) are all REVERSED in the shipped platform. What remains useful
> here as an **authoring reference**: the field → schema.org property mappings (§3–§4),
> the bilingual language-map handling (§5), and the `jawafdehi:` extension-namespace
> terms (§7). Live state: `../../ARCHITECTURE.md`.

# NES → schema.org Mapping (Design)

Status: design + Phase-2 implementation of `to_jsonld()` (serializer + tests landed; endpoint wiring done behind `?format=jsonld`).
Scope: the `nes_service` app, trunk `main`. NES is the canonical entity model for the Jawafdehi platform.

## 1. Goal & approach

We want NES entities to be expressible as [schema.org](https://schema.org) types so the data is
interoperable and linked-data friendly (JSON-LD), WITHOUT:

- breaking the existing entity API response shape (default `model_dump(mode="json")`), or
- breaking the canonical id contract `entity:<entity_prefix>/<slug>`.

**Decision: serialization layer, not a remodel.** We add a read-time mapper
`to_jsonld(entity) -> dict` that projects the stored Pydantic model into a
schema.org-shaped JSON-LD document. The stored model is unchanged; the canonical id
is preserved verbatim as the JSON-LD `@id` (as an IRI, see §6). This is strictly
lower risk than remodeling: no migration, no validator churn, existing
`/api/entities/{id}` consumers see no change. JSON-LD is opt-in.

Rejected alternative — remodeling NES fields to schema.org names (e.g. renaming
`birth_date`→`birthDate`, collapsing `names`→`name`/`alternateName`): it would
break every existing reader/writer, the JSONB store, the bulk-ingest mappers, and
the PATCH (RFC-6902) blocked-path logic. Not worth it; schema.org is a *publishing*
vocabulary, NES stays the *authoring* model.

## 2. Output representation

`to_jsonld(entity)` returns a `dict` with:

- `@context`: a context object that (a) pins the schema.org vocabulary as default,
  and (b) declares the Jawafdehi extension namespace `jawafdehi:` for terms
  schema.org has no equivalent for (§7). The `@context` also defines the
  bilingual `name`/`description` handling as language-tagged values (§5).
- `@type`: one or more schema.org types (string or array) — see §3.
- `@id`: the canonical NES id rendered as an IRI (§6).
- schema.org properties mapped from NES fields (§4).
- `jawafdehi:*` extension properties for Nepal-specific data with no schema.org home (§7).

Exposure: `GET /api/entities/{id}?format=jsonld` returns the JSON-LD document
(`application/ld+json`-shaped dict via DRF `Response`); without the param the
response is byte-for-byte the existing `model_dump(mode="json")`. List/batch
endpoints are unchanged (JSON-LD is detail-only for now; graph/`@graph` output for
collections is a documented follow-up).

### Example context

```json
{
  "@context": [
    "https://schema.org",
    {
      "jawafdehi": "https://jawafdehi.org/ns#",
      "name": { "@id": "schema:name", "@container": "@language" },
      "alternateName": { "@id": "schema:alternateName", "@container": "@language" },
      "description": { "@id": "schema:description", "@container": "@language" }
    }
  ]
}
```

## 3. EntityType / EntitySubType → schema.org `@type`

Driver: the entity's `type` plus `sub_type`/`entity_prefix`. We pick the most
specific schema.org type available and, where schema.org lacks a precise type, emit
a base type plus an `additionalType` (an IRI into the `jawafdehi:` namespace) so the
Nepal-specific classification is not lost.

| NES type | NES sub_type / prefix | schema.org `@type` | `additionalType` (jawafdehi:) |
|---|---|---|---|
| `person` | (none) | `Person` | — |
| `organization` | (bare) | `Organization` | — |
| `organization` | `government_body` / `government` | `GovernmentOrganization` | — |
| `organization` | `ngo` | `NGO` | — |
| `organization` | `political_party` | `Organization` | `jawafdehi:PoliticalParty` *(schema.org has no party type)* |
| `organization` | `hospital` | `Hospital` | — |
| `organization` | `private_company` | `Corporation` | — |
| `organization` | `contractor` | `Organization` | `jawafdehi:Contractor` |
| `organization` | `judicial_body` | `GovernmentOrganization` | `jawafdehi:JudicialBody` *(see note)* |
| `organization` | `international_org` | `Organization` | `jawafdehi:InternationalOrganization` |
| `location` | (bare) | `Place` | — |
| `location` | `province` / `district` / `municipality` / `metropolitan_city` / `sub_metropolitan_city` / `rural_municipality` / `ward` | `[ "Place", "AdministrativeArea" ]` | `jawafdehi:Province` etc. (the exact admin tier) |
| `location` | `constituency` | `[ "Place", "AdministrativeArea" ]` | `jawafdehi:ElectoralConstituency` *(electoral, not administrative)* |
| `project` | `development_project` | `[ "Project", "GovernmentService" ]` | `jawafdehi:DevelopmentProject` |

Notes / decisions:

- **political_party**: schema.org has *no* political-party type. We keep `@type:
  Organization` and add `additionalType: jawafdehi:PoliticalParty`. (Wikidata uses
  `Q7278`; we expose `jawafdehi:PoliticalParty` and let the Wikidata link ride in
  `identifier`/`sameAs`, §4.)
- **judicial_body**: schema.org has `Courthouse` (a `CivicStructure`/place — the
  *building*) but no "court as an organization" type. A court in NES is an
  organization, so we map to `GovernmentOrganization` + `additionalType:
  jawafdehi:JudicialBody`. (Emitting `Courthouse` would wrongly type the org as a
  physical place.)
- **project**: schema.org `Project` is generic; these are government development
  projects, so we emit `[ "Project", "GovernmentService" ]` and tag the precise
  Nepal class via `additionalType`. `GovernmentService` carries the
  donor/agency/budget context better than bare `Project`.
- **AdministrativeArea**: each admin tier (province/district/…/ward) is
  `[ "Place", "AdministrativeArea" ]`; the precise tier is carried in
  `additionalType` because schema.org does not distinguish province vs. ward.

## 4. Field → schema.org property mapping

Common to all entities (from `Entity` base):

| NES field | schema.org property | Notes |
|---|---|---|
| `id` (`entity:<prefix>/<slug>`) | `@id` | rendered as IRI, §6 |
| `names` (kind=PRIMARY) | `name` | language map `{ en, ne }`, §5 |
| `names` (other kinds) + `misspelled_names` | `alternateName` | language map, multiple values, §5 |
| `short_description` / `description` | `description` | language map; `description` preferred, falls back to `short_description` (the short form also emitted as `disambiguatingDescription`) |
| `identifiers[]` (`ExternalIdentifier`) | `identifier` (`PropertyValue`) + `sameAs` | each becomes a `PropertyValue {propertyID: scheme, value}`; when a `url` is present it is also added to `sameAs` (so Wikidata/Wikipedia/social links are crawlable). `website` scheme → `url`. |
| `pictures[]` (`EntityPicture`) | `image` (`ImageObject`) | `{ @type: ImageObject, contentUrl, width, height, caption }`; `thumb` type → `thumbnailUrl` |
| `contacts[]` (`Contact`) | `contactPoint` / `email` / `telephone` / `sameAs` | EMAIL→`email`, PHONE→`telephone`, URL→`url`, social handles→`sameAs` (URL contacts) |
| `tags[]` | `keywords` | plain string list (project `tags` are structured — handled in §7) |
| `attributions[]` (`Attribution`) | `jawafdehi:attribution` | no clean schema.org analogue (`citation`/`isBasedOn` expect CreativeWork URLs); kept as extension |
| `created_at` | `dateCreated` | ISO-8601 |
| `version_summary` | `jawafdehi:version` | internal versioning metadata; extension |
| `entity_prefix`, `slug`, `type`, `sub_type` | `jawafdehi:entityPrefix`, `jawafdehi:slug`, … | the NES classification, preserved as extension so the doc round-trips conceptually |
| `attributes` (free dict) | `additionalProperty` (`PropertyValue[]`) | each key/value → `PropertyValue` |

Person (`PersonDetails`, `ElectoralDetails`):

| NES field | schema.org | Notes |
|---|---|---|
| `personal_details.birth_date` | `birthDate` | partial dates ("2012", "2012-01") pass through as-is (schema.org `Date` allows year/year-month) |
| `personal_details.birth_place` (`Address`) | `birthPlace` (`Place`) | if `location_id` set, emit `Place` with `@id` = location IRI; else `Place` with `address` text |
| `personal_details.gender` | `gender` (`GenderType`) | male→`schema:Male`, female→`schema:Female`, other→free text "other" |
| `personal_details.address` (`Address`) | `address` (`PostalAddress`) | `location_id`→`addressRegion`/`@id` ref; description→`streetAddress` |
| `personal_details.citizenship_place` | `jawafdehi:citizenshipPlace` | "place of citizenship registration" ≠ schema.org `nationality`; extension (district-level) |
| `personal_details.father_name` / `mother_name` | `parent` (`Person` with `name`) | two `parent` entries (name-only Persons) |
| `personal_details.spouse_name` | `spouse` (`Person` with `name`) | name-only Person |
| `personal_details.education[]` (`Education`) | `alumniOf` (`EducationalOrganization`) | institution→`name`; degree/field/years → `jawafdehi:` sub-props (schema.org has no degree-on-alumniOf) |
| `personal_details.positions[]` (`Position`) | `hasOccupation` (`Role`/`Occupation`) + `memberOf` | title→`Occupation.name`; organization→`Role.memberOf`/affiliation; start/end → `startDate`/`endDate` on a `Role` |
| `electoral_details` (`ElectoralDetails`, `Candidacy[]`) | `jawafdehi:electoralDetails` | **no schema.org vocabulary for candidacies/elections** — full extension (§7) |

Organization (subtype-specific):

| NES field | schema.org | Notes |
|---|---|---|
| `address` (`Address`) | `address` (`PostalAddress`) | as above |
| `PoliticalParty.party_chief` | `jawafdehi:partyChief` | extension (prefer relationships) |
| `PoliticalParty.registration_date` | `foundingDate` | closest schema.org analogue |
| `PoliticalParty.symbol` | `jawafdehi:electionSymbol` | electoral symbol; no schema.org term |
| `GovernmentBody.government_type` | `jawafdehi:governmentTier` | federal/provincial/local — extension |
| `Hospital.beds` | `jawafdehi:numberOfBeds` | (schema.org `Hospital` has no beds prop; `availableService` is for services) |
| `Hospital.services` | `availableService` (`MedicalProcedure`/text) | |
| `Hospital.ownership` | `jawafdehi:ownershipType` | Public/Private/Government — extension |
| `PrivateCompany.registration_number` | `identifier` (`PropertyValue` propertyID=`ocr`) | Office of Company Registrar number |
| `PrivateCompany.industry` | `industry` | schema.org `Organization.industry`-style text (uses `naics`/free text) |
| `Contractor.license_number` | `identifier` (`PropertyValue` propertyID=`license`) | |
| `Contractor.grade` | `jawafdehi:contractorGrade` | A/B/C grade — extension |
| `JudicialBody.jurisdiction` | `jawafdehi:jurisdiction` | (schema.org `jurisdiction` exists only on a few legislation types) |

Location (`Location`):

| NES field | schema.org | Notes |
|---|---|---|
| `parent` (entity id) | `containedInPlace` (`Place` with `@id`) | the parent admin area IRI |
| `lat` / `lng` | `geo` (`GeoCoordinates`) | `{ @type: GeoCoordinates, latitude, longitude }` |
| `area` (km²) | `jawafdehi:areaSqKm` | schema.org `area` expects a `QuantitativeValue`; kept simple as extension (could be promoted later) |
| `administrative_level` (computed) | `jawafdehi:administrativeLevel` | 1=province … 4=ward; extension |

Project (`Project`):

| NES field | schema.org | Notes |
|---|---|---|
| `stage` | `jawafdehi:projectStage` | pipeline/ongoing/completed/… — no schema.org enum |
| `implementing_agency` / `executing_agency` | `provider` / `jawafdehi:executingAgency` | implementing→`provider` (org name); executing→extension |
| `financing[]` (`FinancingCommitment`) | `jawafdehi:financing` | donor/amount/terms — extension (schema.org has `MonetaryGrant`/`funding` but loses the DFMIS structure; kept as extension, with a `funder`/`funding` summary as a follow-up) |
| `total_commitment` / `total_disbursement` | `jawafdehi:totalCommitment` / `jawafdehi:totalDisbursement` | extension (currency-typed promotion is a follow-up) |
| `dates[]` (`ProjectDateEvent`) | `jawafdehi:dates` | typed milestones (APPROVAL/START/…); no schema.org analogue |
| `sectors[]` / `tags[]` (`CrossCuttingTag`) | `jawafdehi:sectors` / `jawafdehi:crossCuttingTags` | donor taxonomy mappings; extension |
| `project_url` | `url` | |
| `donor_extensions[]` | (omitted from JSON-LD) | raw donor payloads — internal traceability only, not published |

## 5. Bilingual (en/ne) name & text handling

NES stores names as `{ en: NameParts, ne: NameParts }` (each with `full` plus
optional given/family) and free text as `LangText { en: {value}, ne: {value} }`.

**Decision: JSON-LD language maps** (`@container: @language`) for `name`,
`alternateName`, `description`. This is the cleanest JSON-LD-1.1 representation and
round-trips to language-tagged literals:

```json
"name": { "en": "Ram Bahadur Thapa", "ne": "राम बहादुर थापा" }
```

- For `name` we use the PRIMARY name's `full` per language. Structured parts
  (given/family) are additionally emitted as `givenName`/`familyName` from the
  English parts only (schema.org `givenName`/`familyName` are not language-mapped;
  using English/romanized avoids ambiguity; Nepali parts ride inside the `name`
  language map).
- `alternateName` collects all non-primary names + misspelled names; because a
  language map holds one value per language, multiple alternates are emitted as an
  **array of language maps** (JSON-LD permits this and processors flatten it to
  multiple language-tagged literals).
- Empty languages are omitted (no `null` keys).

This keeps the `ne` text first-class and crawler/JSON-LD-processor friendly without
inventing per-language properties.

## 6. `@id` and the canonical id contract

The NES id `entity:person/ram-bahadur` is the canonical contract and **must not
change**. In JSON-LD, `@id` should be an IRI. We render:

```
@id = "https://jawafdehi.org/entity/person/ram-bahadur"
```

i.e. the `entity:` scheme is mapped to the resolvable Jawafdehi base
`https://jawafdehi.org/entity/` + the `<prefix>/<slug>` path. The original opaque id
is *also* preserved as `jawafdehi:entityId: "entity:person/ram-bahadur"` so the
canonical contract is recoverable from the JSON-LD verbatim. References to other
entities (location `parent`, person `birth_place.location_id`, etc.) use the same
IRI mapping for their `@id`.

Base IRI is a single constant (`JAWAFDEHI_BASE_IRI`) so it can be repointed without
touching the mapping logic.

## 7. Nepal-specific extension terms (`jawafdehi:` namespace)

Namespace: `jawafdehi: https://jawafdehi.org/ns#`. Used wherever schema.org has no
faithful term. Anything under `jawafdehi:` is explicitly **out of scope for
schema.org interop** and is there for completeness/round-trip.

Extension terms (current):

- Classification: `jawafdehi:entityId`, `jawafdehi:entityPrefix`, `jawafdehi:slug`,
  `jawafdehi:subType`, plus `additionalType` IRIs (`jawafdehi:PoliticalParty`,
  `jawafdehi:JudicialBody`, `jawafdehi:Province`, `jawafdehi:DevelopmentProject`, …).
- Person: `jawafdehi:citizenshipPlace`, `jawafdehi:electoralDetails` (candidacies,
  constituencies, vote counts, election symbols — entirely Nepal/EC-specific).
- Org: `jawafdehi:partyChief`, `jawafdehi:electionSymbol`,
  `jawafdehi:governmentTier`, `jawafdehi:numberOfBeds`, `jawafdehi:ownershipType`,
  `jawafdehi:contractorGrade`, `jawafdehi:jurisdiction`.
- Location: `jawafdehi:areaSqKm`, `jawafdehi:administrativeLevel`.
- Project: `jawafdehi:projectStage`, `jawafdehi:executingAgency`,
  `jawafdehi:financing`, `jawafdehi:totalCommitment`,
  `jawafdehi:totalDisbursement`, `jawafdehi:dates`, `jawafdehi:sectors`,
  `jawafdehi:crossCuttingTags`.
- Provenance: `jawafdehi:attribution`, `jawafdehi:version`.

**Bikram Sambat (BS) dates.** NES currently stores dates as Gregorian
(`date`/ISO strings), so the serializer emits Gregorian into `birthDate`,
`foundingDate`, etc. If/when BS dates are stored alongside, the convention is to
keep the Gregorian value in the schema.org property and add a parallel
`jawafdehi:birthDateBS` (string, BS calendar) extension term. Documented here so the
namespace is reserved; not implemented (no BS data in the model today).

## 8. What was implemented vs. designed

Implemented (Phase 2):

- `nes_service/core/schemaorg.py` — `to_jsonld(entity) -> dict` covering Person,
  Organization (+ all subtypes), Location, Project, with the `@context`, `@type` /
  `additionalType`, `@id` IRI mapping, bilingual language maps, and the `jawafdehi:`
  extensions above.
- `GET /api/entities/{id}?format=jsonld` returns the JSON-LD document; default
  response unchanged.
- Tests in `tests/test_schemaorg.py` (unit, on `to_jsonld`) + an API test for the
  `?format=jsonld` path. Existing 26 tests stay green.

Designed / follow-ups (NOT implemented):

- `@graph` / JSON-LD output for the list & batch endpoints.
- Promoting `area`, `total_commitment`, financing to typed schema.org
  `QuantitativeValue` / `MonetaryGrant` / `funding` (currently kept as
  `jawafdehi:` extensions to avoid lossy mapping).
- BS-date parallel terms (no source data yet).
- Publishing the `jawafdehi:` context/vocabulary at `https://jawafdehi.org/ns#`
  (the IRIs are reserved; the document is a stub until then).
