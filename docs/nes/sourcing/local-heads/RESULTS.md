# LOCAL-UNIT ELECTED HEADS (mayors/chairs + deputies) — 2022 local election — RESULTS

Real acquisition, 2026-06-29. Companion to the ward-chairs wave: the **executive head
and deputy head of each of Nepal's 753 local units** from the 2022 (2079 BS) local-level
election. Same proven ECN static-JSON data path, the OTHER posts. Read-only on all
sources; the live DB was NOT touched (orchestrator ingests).

## Posts harvested (from `VdcPost.json`)

`VdcPost.json` carries one `postname` per `postid`, nationwide:

| postid | postname (ne) | Role | bodies | bucket |
|---:|---|---|---:|---|
| 1 | अध्यक्ष | rural-municipality **Chairperson** | 460 | HEAD |
| 2 | उपाध्यक्ष | rural-municipality **Vice-chairperson** | 460 | DEPUTY |
| 3 | प्रमुख | municipality/city **Mayor** | 293 | HEAD |
| 4 | उपप्रमुख | municipality/city **Deputy Mayor** | 293 | DEPUTY |
| 5 | वडा अध्यक्ष | ward chair | 753 | **EXCLUDED** (done — ward-chairs wave) |
| 6/7/8 (+9..12) | सदस्य / महिला सदस्य / दलित महिला सदस्य | ward members | 753 | EXCLUDED |

460 (rural) + 293 (municipal) = 753 local bodies. **HEAD = {1,3}, DEPUTY = {2,4}.** The
`postid` alone fixes both the unit type (rural vs municipal) and the role title, so role
labels are read straight from the postid — no inference from the location `@type` needed
(it agrees anyway: locations carry 460 `RuralMunicipality` vs 276 `Municipality` + 17
`MetropolitanCity`/`SubMetropolitanCity` = 293). Head/deputy rows carry `Ward=null` and
there is **exactly one** "Elected" (`निर्वाचित`) winner per head/deputy post per body
(verified: 0 multi-winner bodies upstream).

## Heads/deputies sourced vs ~753 each

| Quantity | Count |
|---|---:|
| HEAD winners present in ECN (postid 1/3, "Elected") | **749** (457 chairs + 292 mayors) |
| DEPUTY winners present in ECN (postid 2/4, "Elected") | **748** (456 vice-chairs + 292 deputy mayors) |
| **Published Person records** | **1,497** |

**Coverage gap to 753 each — fully explained, none fabricated:**

- **HEAD missing = 4 of 753.** 3 are remote Dolpa high-mountain gaunpalikas with no ECN
  winner row (postponed/never-posted 2022 poll): Kaike (काईके), Charka Tangsong
  (छार्का ताङसोङ), She-Phoksundo (शे फोक्सुन्डो). The 4th is **Dodhara Chandani
  Nagarpalika (Kanchanpur)** — the known **752-vs-753 off-by-one** the offices-local wave
  flagged: this unit is absent from the ingested local-unit office/location data, so its
  head/deputy cannot be linked and are held rather than emitted with a dangling
  `memberOf`. (ECN does carry a winner for it; it is the only join miss.)
- **DEPUTY missing = 5 of 753.** Same 3 Dolpa units + Dolpo Buddha (डोल्पो बुद्ध, Dolpa)
  + Narpa Bhumi / Nar-Phu (नार्पा भूमि, Manang) which have no deputy winner row, plus the
  Dodhara Chandani join miss. (These mountain units are the same postponed-poll set the
  ward-chairs wave documented.)

These are the identical remote-mountain postponed-poll units (Dolpa / Manang) the
ward-chairs wave already documented; we do NOT invent winners for them.

## Sourcing — ELECTION-AUTHORITY EXCEPTION (adopted policy)

ECN is the election authority and is **sufficient** for elected officials. Each of the
1,497 records carries **two ECN `sources` artifacts**, both
`authority = "result.election.gov.np"`, so the orchestrator's
`promote_held --election-authority` publishes them:

1. the per-body result JSON URL — `…/JSONFiles/Election2079/Local/<bodyId>.json`
2. the certified result-sheet PDF — `…/matpatra-pdf/1-<bodyId>-<postid>-0.pdf`
   (same `/matpatra-pdf/1-<body>-<postid>-<ward>` pattern as the ward-chairs PDFs; head/
   deputy posts have no ward, so the ward slot is `0`).

Unlike the ward-chairs wave (which predated the exception and flagged
`single-source-hold`), these records carry **no HOLD flag** — the election-authority
exception governs elected officials.

## Validation (platform validators)

Run from `/damodaha-volunteer/jawafdehi-platform`:
`TESTING=true DJANGO_SETTINGS_MODULE=monolith.config.settings uv run python` →
`validate_jsonld_entity` + `is_valid_entity_iri`.

| Check | Result |
|---|---|
| `validate_jsonld_entity` PASS | **1,497 / 1,497** |
| `is_valid_entity_iri` PASS | **1,497 / 1,497** |
| Duplicate IRIs | **0** |
| `memberOf` → resolves to an ingested local-unit OFFICE IRI | **1,497 / 1,497 (100%)** |
| `containedInPlace` → resolves to an ingested local-unit LOCATION IRI | **1,497 / 1,497 (100%)** |

## Join to the ingested entities

ECN local-body id (`5001..`) **≠** CBS local-unit code (e.g. `10101`). The bridge is the
ward-chairs wave's **(district IRI + fuzzy-Devanagari palika name)** join, reusing its
`_ne_stem` / `_ne_collapse`. Once a body maps to a local unit (its CBS code), the head's:

- `memberOf` → the local-unit **OFFICE** IRI
  (`…/organization/government/localunit/<slug>-<cbs>`), keyed by `cbs-local-unit-code`
  from `offices-local/localunit_offices.json`.
- `containedInPlace` → the local-unit **LOCATION** IRI
  (`…/location/localunit/<slug>-<cbs>`), keyed by CBS from
  `offices-local/localunit_locations.json`.

**752 / 753 local bodies joined** (only Dodhara Chandani Nagarpalika unjoinable — the
known off-by-one, not present in the ingested data). Every emitted record links both
office and location (100%).

**Party join** (`PoliticalPartyName` ne → parties wave `name.ne`, collapsed key folding
ँ/ं, ष/स, व/ब, ि/ी, ई/इ, parens; UML abbreviation `(एमाले)` aliased to the full name):
**1,480 party-linked + 17 Independent (स्वतन्त्र); 0 unmatched.**

## Record shape (per brief)

`@type "Person"`; IRI `…/entity/person/<romanized-name>-<cbs>-<mayor|chair|deputy|
vicechair>`; bilingual `name` {ne,en}; `hasOccupation` Role
`roleName "<Mayor|Chairperson|Deputy Mayor|Vice-chairperson> of <Unit>"` with
`jobTitle` {en, ne=मेयर/उपमेयर for municipalities, अध्यक्ष/उपाध्यक्ष for rural},
`memberOf` → local-unit office IRI (+ `jawafdehi:party` / `jawafdehi:partyOrg` IRI);
`containedInPlace` → local-unit location IRI; `jawafdehi:electionCycle "2022 local"`;
`jawafdehi:branch "local-government"`; `identifier` `ecn-candidate-id`; plus
`jawafdehi:gender`, `jawafdehi:votesReceived`.

## Files

- `normalize_local_heads.py` — the builder (pure + offline given the `/tmp/ecn2079`
  snapshot from the ward-chairs wave's `fetch_ecn_2079_local.sh`).
- `local_heads_records.json` — `{"records":[...]}` (1,497 records), ready for `bulk_ingest`.

## Reproduce

```bash
# snapshot from the ward-chairs wave (if not already at /tmp/ecn2079):
bash ../ward-chairs/fetch_ecn_2079_local.sh /tmp/ecn2079
python3 normalize_local_heads.py            # -> local_heads_records.json
```
