# entity_labels.jsonl

140 hand-labelled rows for scoring the **resolver** — `casework/entity_resolver.py`, the step
that turns an already-extracted name into an NES entity id. One row per extracted name:

| Field | Meaning |
|---|---|
| `extracted` | the name exactly as the extraction produced it |
| `expected_verdict` | `BIND` or `NO_MATCH` |
| `expected_nes_id` | the NES `@id` a `BIND` must land on, `null` for `NO_MATCH` |
| `provenance` | why the label is trusted — a human bind on a named case, or a rejection |

## It does not score the extractor

Nothing here says which names an LLM should have pulled out of a press release or a court
order. So a change to the extraction prompt, or to which slice of a court order the model
sees, cannot be gated on this file — it will score identically either way. That mistake has
been made once already on this fixture.

The precision and recall figures in `casework/enrich_related_entities.py`'s module docstring
were measured on this set.

## Why it ships with no importer

The rows were lost with a deleted commit and recovered by hand. Nothing imports them today,
and that is fine: re-labelling 140 Nepali names against NES costs a day, and the next piece
of resolver work wants them.
