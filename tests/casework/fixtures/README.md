# Resolver fixtures

`entity_candidates.json` is a frozen capture of prod `/api/search/?type=entity` responses.
`entity_labels.jsonl` is the labelled set the resolver is measured against. Together they
make `test_entity_resolver_labelled.py` offline and deterministic — the "same NES snapshot"
the design note requires.

The names are real: 138 come from the July A/B extraction logs in
`work/2026-07-17-enricher-extraction/`, so the distribution matches what the LLM actually
emits, dirt and all. Two more are constructed (see below), for 140 rows.

## The gate is currently RED, on purpose

`test_zero_false_positives_across_the_labelled_set` fails on five rows. Precision is
0.872, not 1.0. Those five are not mislabelled and the threshold is not wrong — the
resolver binds five Election Commission candidate records that are different people who
happen to share a name with someone the case names. Read `.superpowers/sdd/plan/task-5-report.md`
before touching either fixture. **Do not fix this by editing a label or moving
`MIN_BIND_SCORE`.**

## Where each label comes from

The label must not come from the resolver's own logic, or the gate proves nothing. The
resolver reads exactly three strings per candidate: `title.ne`, `title.en`, and the IRI
slug. Every label below rests on something else.

| `provenance` | Evidence | Rows |
|---|---|---|
| `verified: human bind on <case-slug>` | The entity is already bound to that case in prod, by a caseworker or the portal migration. `GET /api/cases/<slug>/` | 19 |
| `verified: prod entity doc consistent with case context` | The full entity document pins the referent — a CBS local-unit code, `gov-np-domain`, a wikidata QID, or CIAA-portal-backlog provenance — and the case's district agrees | 20 |
| `rejected: ECN election-candidate record, not the case subject` | The document carries `ecn-candidate-id` and a 2079 candidate `hasOccupation` for a ward the case has nothing to do with | 5 |
| `rejected: N same-name entities in prod` | Self-verifying: N real entities, one name | 10 |
| `rejected: no NES entity with this name` | Nothing scored above zero against the frozen capture | 83 |
| `unsettled: evidence does not decide` | Labelled conservatively — REVIEW, never BIND | 1 |
| `constructed: <shape>` | Added to cover a false-positive shape the 138 lack | 2 |

Seven of the twenty logged cases are readable in prod (two PUBLISHED, five IN_REVIEW); the
other thirteen are DRAFT and need auth. On those seven, 15 of 15 proposed binds onto a
CIAA-portal-backlog entity were already bound to that very case by a human, and 0 of 5 onto
an Election Commission record were. That 15/15 against 0/5 is what justifies the two
provenance rules that read entity-document provenance.

## The six false-positive shapes

| Shape | Extracted | Expected |
|---|---|---|
| Two NES people, one name | `अनिष श्रेष्ठ` | `REVIEW` (constructed) |
| Shared surname, different given name | `अनुप कुमार खत्री` | `NO_MATCH` |
| Partial match, surname differs | `घुरनी देवी खत्वे` | `NO_MATCH` |
| Absent from NES | `खगेन्द्र पराजुली` | `NO_MATCH` |
| Organisation resembling a person | `मान बहादुर भण्डारी` | `REVIEW` |
| One character different | `अनिष श्रेष्ट` | `NO_MATCH` (constructed) |

The two constructed names live in `EXTRA_SHAPES` in the capture script, not in the names
file, because they are not recoverable from the extraction logs.

## Regenerating the capture

```bash
uv run python scripts/capture_entity_candidates.py \
    ../../work/2026-08-03-Fix-related_entities-enricher/extracted_names.txt \
    tests/casework/fixtures/entity_candidates.json
```

That is a GET-only production read and takes about 80 seconds. Recover the names file
first if it is missing:

```bash
cd /home/gaurav/repos/jawafdehi-meta
grep -rhoP '\[DRY RUN\] (location|related)\s+\K.*' \
  work/2026-07-17-enricher-extraction/ab_run/*.log \
  work/2026-07-17-enricher-extraction/ab_rerun_prose*/*.log \
  | sed 's/  *—.*//' | sort -u \
  > work/2026-08-03-Fix-related_entities-enricher/extracted_names.txt
```

Re-verify by hand any label whose candidate list changed — NES grows, and a name that was
unambiguous can gain a namesake. `work/2026-08-03-Fix-related_entities-enricher/make_labels.py`
regenerates `entity_labels.jsonl` from the recorded evidence and asserts the ECN/portal
split still holds against the live documents.
