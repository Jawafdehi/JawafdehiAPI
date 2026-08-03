"""Prompt-context formatters, shared by every enricher that builds a prompt
out of a case's already-populated fields.

Ported VERBATIM from the deleted `casework/common.py` (donor commit `0321a85`,
`format_bigo` / `format_list` / `format_entities`). They live here, not in an
enricher, because in the donor they were genuinely shared: `enrich_description`,
`enrich_title` and `enrich_card` all imported the same three functions, so a
per-enricher copy is not a port -- it is a fork, and the first thing that
happens to a fork is that one side's `(unknown)` becomes the other side's
`उल्लेख छैन` and the two prompts stop agreeing about what a missing बिगो looks
like.

`format_entities` READS THE LIVE PAYLOAD, and it is worth being explicit about
why it reads the keys it does. A case's `entities` entry is
`{nes_id, display_name, entity_type, type, outcome, notes}` -- built by
`cases/services/nes_resolver.py::build_entity_binds` and served by
`CaseSerializer.get_entities`. `type` is the RELATIONSHIP type (accused /
related / location), which is why it is what gets bracketed, and `entity_type`
(person / organisation, from NES) is deliberately not in the line: the donor
did not include it. There is no `role` key and no `entity_iri` key on this
payload; reading those yields a bulleted list of blanks that looks plausible in
a prompt and silently starves the model of every name in the case.
"""


def format_bigo(bigo) -> str:
    """Render the बिगो for a prompt: thousands-separated NPR, or '(unknown)'."""
    try:
        value = int(bigo)
    except (TypeError, ValueError):
        return "(unknown)"
    return f"{value:,}" if value > 0 else "(unknown)"


def format_list(items) -> str:
    """Format a list of strings as prompt bullets, or '(none provided)'."""
    if not items:
        return "(none provided)"
    return "\n".join(f"- {x}" for x in items)


def format_entities(entities) -> str:
    """Format the case entities (accused/related/location) as prompt bullets.

    A non-dict entry is skipped rather than crashing prompt assembly -- one
    malformed row must not cost the whole case its entity context.
    """
    if not entities:
        return "(none provided)"
    lines = []
    for e in entities:
        if not isinstance(e, dict):
            continue
        name = e.get("display_name") or ""
        etype = e.get("type") or ""
        notes = e.get("notes") or ""
        line = f"- [{etype}] {name}"
        if notes:
            line += f" — {notes}"
        lines.append(line)
    return "\n".join(lines) or "(none provided)"
