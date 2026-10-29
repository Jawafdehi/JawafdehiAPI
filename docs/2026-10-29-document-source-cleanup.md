# Document-source tokens that need a merge or a correction

*2026-10-29. Companion to the labelling pass in `materials/sources.py`.*

Four of the 34 tokens in `/api/document-sources/` are not naming problems. They
are ingest accidents: three duplicate a token that already exists, and one is
malformed.

They **are** labelled in `materials/sources.py`, because they hold real
documents — an unlabelled token with 33 charge sheets behind it helps nobody,
and a reader who lands on one still deserves to be told what it is. Each reuses
the attribution already reviewed for the token it duplicates (a test pins that,
so the two cannot drift into making different claims about one shelf) and each
description states the pending merge outright, so the duplication stays visible
in the payload rather than being smoothed over by a tidy name.

**Delete those four entries when the merges land.** A label outliving its token
is worse than no label: it advertises a shelf that is empty.

The one genuinely unlabelled token is `cib` — see the end of this document.

All evidence below is read from the materials' own fields via
`GET /api/materials/?source=<token>`, which returns every row for these small
tokens.

## Why these tokens exist at all

Eight of the 34 source tokens are a verbatim **`MaterialType` value**
(`materials/jsonld.py`) sitting in the `source` column:

> `court_order`, `news`, `official_report`, `document`, `charge_sheet`,
> `press_release`, `legal_corpus`, `social_media`

**The mechanism is that nothing constrains the token.** `source` is derived from
the `@id` that the *caller* builds (`materials/single_source_ingest.py`), and the
only validation is shape: `is_valid_material_iri` checks a regex, the host and a
length cap. **There is no allowlist of known source tokens anywhere in the
codebase.** The admin upload component takes `source` as a plain field
(`MaterialFileUpload.tsx`). So any string of the right shape mints a new source
on first write, and it appears in `/api/document-sources/` as a new shelf.

Given that freedom, uploaders reach for the document's type word. That is a
human pattern, not a code default — worth stating precisely, because it puts the
fix in a different place (validate the token at the write path) than "change a
server-side default" would.

Being a type name is not by itself a defect — `court_order` (43,125) and
`official_report` (223) are type names and are the established, reviewed
shelves. The test is **whether a more specific source already exists to absorb
the rows.** For `charge_sheet` and `press_release` it does, which is exactly
what makes those two accidents. For `news`, `document`, `legal_corpus` and
`social_media` it does not, so they stay and are labelled for what they are.

This also explains, without any further investigation, why those four carry no
publisher: they were never a source.

## The discriminator

Every material carries `jawafdehi:sourceType`, set by the shaper that ingested
it. It identifies the document CLASS independently of the `source` token, which
makes it the right thing to compare tokens on — two tokens carrying the same
`sourceType` and the same `additionalType` hold the same documents regardless of
what they are called.

| token | n | `@type` | `additionalType` | `jawafdehi:sourceType` |
|---|---|---|---|---|
| `ag` | 99,750 | `DigitalDocument` | `jawafdehi:ChargeSheet` | `AG_ABHIYOG_PATRA` |
| **`charge_sheet`** | **33** | `DigitalDocument` | `jawafdehi:ChargeSheet` | `AG_ABHIYOG_PATRA` |
| **`chargesheet`** | **2** | `DigitalDocument` | — | `AG_ABHIYOG_PATRA` (1 of 2) |
| `ciaa_press_release` | 3,556 | `CreativeWork` | `jawafdehi:PressRelease` | `CIAA_PRESS_RELEASE` |
| **`press_release`** | **24** | `CreativeWork` | `jawafdehi:PressRelease` | `CIAA_PRESS_RELEASE` |
| `special_court_order` | 1 | `DigitalDocument` | — | `COURT_ORDER` |
| **`court_order_0133`** | **1** | `DigitalDocument` | — | `COURT_ORDER` |

Rows in bold are the accidents. Sampling: all 33, 2, 24 and 1 rows respectively;
50-row samples for `ag` and `ciaa_press_release`, uniform in both.

---

## 1. `charge_sheet` (33) and `chargesheet` (2) → merge into `ag`

**Three spellings of one thing.** All 33 `charge_sheet` rows are
`jawafdehi:ChargeSheet` / `AG_ABHIYOG_PATRA` — byte-identical in class to the
99,750 rows under `ag`. Their own names say what they are:
`अभियोगपत्र`, `विशेष अदालतमा अख्तियारले दायर गरेको अभियोगपत्र`,
`CIAA Charge Sheet – 081-CR-0090`.

Both `chargesheet` rows are CIAA charge sheets filed at the Special Court
(Melamchi `080-CR-0106`, NTA MDMS `080-CR-0141`); one carries the
`AG_ABHIYOG_PATRA` sourceType and the other carries none at all.

**Confirmed from the document text, not just the metadata.** The transcript
attached to `charge_sheet/20260324.60019b1d` carries YAML front-matter reading
`source_type: "AG_ABHIYOG_PATRA"` and `title: "अभियाेग पत्र"`, and its body opens
`श्री विशेष अदालत, काठमाडौं समक्ष पेश गरेको आरोपपत्र` — a charge sheet presented to
the Special Court on the report of CIAA investigation officers, with नेपाल सरकार
as वादी. That is the `ag` class, read off the document itself.

**Why it happened.** `ag` is minted by the scraper in `materials/sourcing/ag/`,
which always writes the same token. These 35 are uploads, which do not go
through that shaper — the uploader supplies the token, picking the type word
(`charge_sheet`) or a spelling of their own (`chargesheet`). Nothing validates
it against the existing set, so all three coexist.

**Proposed correction.** Re-token all 35 rows to `ag`. The `@id` is the canonical
stored form (`/material/<source>/<ident>`), so this rewrites 35 IRIs and needs
the usual redirect-or-rewrite handling for anything referencing them — check
case evidence links before running it.

## 2. `press_release` (24) → merge into `ciaa_press_release`

Same shape, same cause. All 24 are `jawafdehi:PressRelease` /
`CIAA_PRESS_RELEASE`, several naming themselves `अख्तियारको प्रेस विज्ञप्ति`.

One extra signal worth recording: all 50 sampled `ciaa_press_release` rows carry
a populated `publisher` (`अख्तियार दुरुपयोग अनुसन्धान आयोग`), and **none** of the
24 `press_release` rows carry one. That is the scraper's shaper setting a field
the upload path does not — independent confirmation that these are the same
documents arriving by a different door, and a reason to backfill `publisher`
during the merge rather than only changing the token.

**This one was not in the original brief.** It surfaced from the `sourceType`
comparison, which is the argument for using that field rather than token
spelling as the discriminator.

## 3. `court_order_0133` (1) → correct to `special_court_order`

A malformed token: the case number leaked into it. Its single row is
`material/court_order_0133/2081`, named `Court Order - 080-CR-0133` /
`अदालतको फैसलाः ०८०-cr-०१३३`.

The fix is visible one token over. `special_court_order` holds exactly one row,
`material/special_court_order/0066`, named `Court Order - 080-CR-0066` — the same
kind of upload (a Special Court full-text judgement), same `COURT_ORDER`
sourceType, but with the case number correctly in the **ident** rather than the
source. So `court_order_0133/2081` should be `special_court_order/0133`.

**Their attached documents settle it.** Both rows' `associatedMedia` RAW links
point at the same host and the same upload path, with the same filename shape:

| row | original document |
|---|---|
| `special_court_order/0066` | `supremecourt.gov.np/cp/assets/uploads/special_9807.doc` |
| `court_order_0133/2081` | `supremecourt.gov.np/cp/assets/uploads/special_10248.doc` |

Same publisher, same series, same `special_<n>.doc` convention on the Supreme
Court case portal. These are two documents from one source wearing two tokens.

Left as it is, the token count grows by one for every judgement uploaded this
way, and `/api/document-sources/` fills with single-row sources.

**Guard worth adding.** A token matching `^court_order_\d+$` — or more generally
any new token that is an existing token plus a numeric suffix — should be
rejected at ingest. This is a typo class, not a source.

---

## 4. SEPARATE AND MORE SERIOUS — four materials share one transcript, and it belongs to none of them

Found while following `associatedMedia` to read these documents. This is not a
token problem and should be triaged on its own.

Four materials — **two charge sheets and two court orders**, different document
classes entirely — all point their `MARKDOWN` link at the **same** object:

```
s3.jawafdehi.org/case_uploads/185d4d1f674952d3690746b73b182d1c5b8ffb0f76c439ced054c3995492e89f.md
    ← material/chargesheet/melamchi.fdcbffb4      (Melamchi,  080-CR-0106)
    ← material/chargesheet/20260430.mdms          (NTA MDMS,  080-CR-0141)
    ← material/special_court_order/0066           (           080-CR-0066)
    ← material/court_order_0133/2081              (           080-CR-0133)
```

I downloaded it. It is a **Special Court judgement (फैसला) in case
०81-CR-०095** — naming its bench, `निर्णय नं. 79`, and a `फैसला मिति` of
2083/03/22. It contains no occurrence of `मेलम्ची`, and its case number matches
**none of the four** rows carrying it. (`081-CR-0095` is the case number on a
fifth, unrelated row, `charge_sheet/20260416.84fa060e`.)

**Why this matters more than the token duplication.** Anyone — a caseworker, a
reader, or an LLM told to "open the document and see what it is" — who follows
the transcript on any of these four materials is served the full text of an
unrelated case, with nothing in the response indicating the mismatch. It crosses
document classes, so a charge sheet resolves to a judgement. On an
accountability archive that is a mis-attribution of court text to the wrong
defendants.

**Likely cause:** uploads are keyed on filename rather than content, so two
files uploaded under the same name collide on one object. The merges proposed
above do **not** fix this and should not be taken to have fixed it.

**Suggested next step:** sweep `associatedMedia` across the whole corpus for
`contentUrl`s referenced by more than one material, then compare each
transcript's case number against its parent's. The four above were found in a
seven-token sample, so the real count is likely higher.

---

## How to actually do the merges — order matters

**Do not reach for `move_source` first.** Its own docstring rules this out:

> *"The old public URL 404s afterwards. There is no redirect table for
> materials. That is an accepted cost here … it is the reason this action is
> used once, for a document that is not what its shelf claims, rather than
> offered as a general tidying tool."*

60 rows is not "once". Three steps, in this order:

**1. Close the mint. (Independent, cheapest, do it regardless.)**
Add an allowlist check on the `source` segment at the write path — the one
upsert primitive is the natural place, since every write funnels through it.
Without this, you merge 60 rows and the next upload re-creates the problem; the
`0133` shape also shows the case number leaking in, so reject
`^<known_token>_\d+$` too. This is the only step that is worth doing even if
you decide never to touch the existing rows, and it caps the blast radius while
the rest waits.

**2. Phase 0 from `docs/2026-10-11-material-pretty-idents/PLAN.md`.**
`MaterialIdentHistory` (retired IRI → current), a visibility-gated 301 on a
detail miss, and the upsert consulting it so a write to a retired `@id` is
rewritten rather than reviving the tombstone. That plan's own framing is right:
it *"is what makes `move_source`, the source-token cleanup, and any future
corpus correction reversible instead of one-way"*, and it changes no URLs. On a
CC BY-NC linked-data platform where third parties may have cited an `@id`, the
alternative is 60 permanent 404s.

**3. Then the merge — and fix a gap in `move_source` when you generalise it.**

> **`move_source` does not repoint `CaseMaterialReference`.** Case evidence
> binds to a material by `material_iri` *string* (`cases/models.py`), and the
> action only creates the new row and soft-deletes the old. So re-tokening a
> charge sheet silently orphans the evidence link on every case citing it.

That is worse than the 404, because a 404 is visible and this is not. The
repoint has to happen in the same transaction as the move. **Check this before
anything else:** if these 60 rows are cited by published cases, step 3 is not a
tidying exercise, it is an edit to case evidence.

### There is no urgency

The labels shipped in this PR already fix the user-facing symptom — a model
reading `/api/document-sources/` today is told what `charge_sheet` is and that
it duplicates `ag`. The merge buys tidiness and correct per-shelf counts, not an
outage fix. Nothing here justifies a hurried one-way rewrite of production rows.

---

## Not cleanup, but worth recording

- **`dfmis` transposes its own host.** All 2,117 rows point at
  `dfims.mof.gov.np`, but the token is `dfmis`. The token is the stored IRI
  segment so it is not free to change, and the label now carries the portal's
  spelling in its description. Flagging it so nobody "fixes" one to match the
  other without realising 2,117 `@id`s are involved.
- **`province/koshi` is the only token containing a slash**, which makes its
  `@id` a four-segment path (`/material/province/koshi/<ident>`) where every
  other source yields three. It looks deliberate — the records carry
  `jawafdehi:source: "province/koshi"` and a provincial `spatialCoverage`, so
  the namespace clearly anticipates more provinces — but anything parsing an
  `@id` by splitting on `/` and taking a fixed segment will get this one wrong.
- **One `official_report` row is filed under `document`.**
  `material/document/oag-11353` is the OAG National Risk Assessment Report,
  `@type: Report`, `sourceType: OAG_AUDIT_REPORT`, publisher `नेपाल सरकार`,
  sourced from `oag.gov.np`. It is the only one of 50 sampled `document` rows
  that is not a `MISC` upload. Likely belongs under `official_report`; worth a
  wider sweep of that token for others before moving just this one.
- **`official_report` has 15 series but only 10 are labelled** in
  `SERIES_LABELS`. Out of scope for this pass, but the five unlabelled buckets
  are a smaller version of the same problem and the frontend registry's
  `kindExclude` complement shelf is already absorbing them silently.
- **`cib` (91) is the one token left unlabelled, and it should stay that way
  until someone can read a row.** The authoritative inventory
  (`docs/ngm/ngm-source-inventory.md`) uses "CIB" for two different bodies — the
  Nepal Police **Central Investigation Bureau** (lines 80, 102) and the **Credit
  Information Bureau** (line 123) — and the documents cannot be opened to break
  the tie: all 91 rows are non-`LISTED`, so they return zero from
  `/api/materials/`, zero from `/api/search/` and zero from the MCP
  control-plane search. No row means no `associatedMedia`, so there is no
  document to open. One authenticated read of any single row would settle it,
  since its `url` or `associatedMedia.contentUrl` would name the body outright —
  which is exactly how `news_shilaptra` was settled.
- **`news_shilaptra` is no longer unlabelled**, and the attached document is why.
  Its record names no outlet in any metadata field, but its `associatedMedia`
  RAW link is `shilapatra.com/detail/138491`, and that page's `<title>` is
  byte-identical to the record's Nepali name. The token misspells the outlet
  (`shilaptra`, missing an `a`) — fold it into a correctly-spelled token if the
  news singletons are ever consolidated.
- **An existing plan already flagged part of this.** The meta-repo's
  `docs/2026-10-11-material-pretty-idents/PLAN.md` lists `court_order_0133`,
  `chargesheet` and "singleton news sources" under its risk E14, to be folded in
  "after Phase 0, when it stops being one-way". This write-up adds the evidence
  and two tokens that list missed (`charge_sheet`, `press_release`). Sequence
  the merges behind that plan's Phase 0 rather than ahead of it.
- **There is a working model for the rewrite.**
  `materials/management/commands/fix_oag_corpus.py` already implements a
  `move_source` action: dry-run by default, each correction an action that edits
  in memory and hands back rows for a single write site, so a dry run provably
  cannot write. Extend that rather than writing a new one-off script.
- **`ppmo_blacklist` holds only 4 rows** and its four `url`s point at
  `old.ppmo.gov.np`, which the scraper's own docstring says is dead. The live
  scraper now POSTs to `/api/ingestion/firms/` instead, so these four are
  residue from the retired Scrapy spider rather than a current shelf.
