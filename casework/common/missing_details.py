"""`Case.missing_details` assembly: what the record cites that our evidence lacks.

Two sources, combined here: a DETERMINISTIC floor from what is bound (verifiable,
never wrong), plus up to `MAX_LLM_ITEMS` specific documents the model found
referenced in the sources but absent from our evidence.

Every constant below is measured against the 61 PUBLISHED cases carrying this
field, read 2026-08-07. Items are short Nepali NOUN PHRASES naming a document,
not sentences. Rationale and the corpus numbers:
`docs/superpowers/specs/2026-08-07-missing-details-enricher-design.md` (meta-repo).
"""

import re
from typing import Optional

from casework.common.materials import typed_materials
from casework.common.pipeline import COURT_TYPES

# Copied VERBATIM from published cases rather than composed here.
CHARGE_SHEET_ITEM = "अख्तियार दुरुपयोग अनुसन्धान आयोगले दायर गरेको अभियोगपत्र"
APPEAL_ITEM = (
    "हदम्याद भित्र वादी वा प्रतिवादीले सर्वोच्च अदालतमा पुनरावेदन गरे नगरेको ब्याहोरा"
)

# Enumerator follows ITEM COUNT, matching the corpus: 1 item bare, 2 numerals,
# 3+ letters. Chosen after the model's items are accepted -- see `render`.
# NUMERALS is two entries because only the 2-item branch of `render` indexes it.
NUMERALS = ("१", "२")
LETTERS = ("क", "ख", "ग", "घ", "ङ", "च", "छ", "ज")

CHARGE_SHEET_TYPE = "charge_sheet"
SUPREME_REF = "/courtcase/supreme/"

MAX_LLM_ITEMS = 4     # + 2 floor items = the corpus's densest value (ntc-081-cr-0111, 6)
MAX_CHARS = 700       # SANITY GUARD only -- `MAX_LLM_ITEMS` is the real limit, see below
MAX_ITEM_CHARS = 110  # longer than this is a sentence, not a document name
MAX_ITEMS = 2 + MAX_LLM_ITEMS   # 2 floor + the model's; must stay <= len(LETTERS)

# Words that would NAME a document we already hold, keyed by material type --
# the one grounding rule `reject_item` can check mechanically rather than trust.
# `court_order` must keep the BARE `फैसला` forms: `held_summary` prints
# `विशेष अदालतको फैसला` into the prompt, and echoing that label back is the model's
# most likely wrong answer. Listing only `फैसलाको …` let it through.
HELD_DOC_WORDS = {
    "ciaa_press_release": ("प्रेस विज्ञप्ति", "प्रेस विज्ञप्ती", "प्रेश", "विज्ञप्ति"),
    "press_release": ("प्रेस विज्ञप्ति", "प्रेस विज्ञप्ती", "प्रेश", "विज्ञप्ति"),
    "charge_sheet": ("अभियोगपत्र", "आरोपपत्र", "अभियोग पत्र", "आरोप पत्र"),
    "court_order": ("विशेष अदालतको फैसला", "अदालतको फैसला", "फैसला",
                    "फैसलाको प्रतिलिपि", "फैसलाको पूर्णपाठ", "फैसलाको पुर्णपाठ",
                    "फैसलाको मूलपाठ", "फैसलाको मूल पाठ"),
    "news": ("समाचार",),
}

# We hold the SPECIAL court's verdict, so another court's ruling is legitimately
# missing -- and `अदालतको फैसला` is a substring of `सर्वोच्च अदालतको फैसला`.
OTHER_COURT_WORDS = ("सर्वोच्च", "उच्च अदालत", "पुनरावेदन अदालत")

# The exemption needs a court DOCUMENT, not just a court name. `सर्वोच्च` alone
# appears in APPEAL_ITEM itself, so matching on the name cancelled the
# appeal-restatement rule: `सर्वोच्च अदालतमा पुनरावेदन परेको वा नपरेको विवरण` was
# accepted and published next to the floor item saying the same thing.
# `पुनरावेदनपत्र` and the registry nouns MUST be here -- the appeal petition is the
# single most valuable document to name when an appeal was lodged, and without it
# the appeal-restatement rule dropped it on every case lacking a Supreme reference.
COURT_DOC_WORDS = ("फैसला", "आदेश", "मिसिल", "पर्चा", "इजलास", "अन्तरिम",
                   "पुनरावेदनपत्र", "निवेदन", "अभिलेख", "किताब")

# Substrings marking an item as restating a FLOOR item. Checked only when that
# floor item is actually present -- with a charge sheet bound, a claim about the
# charge sheet's own contents is legitimate.
CHARGE_SHEET_WORDS = ("अभियोगपत्र", "आरोपपत्र", "अभियोग पत्र", "आरोप पत्र")
APPEAL_WORDS = ("पुनरावेदन",)

# Filler the corpus is full of (15 and 17 cases) and a reader cannot act on.
# Multi-word, so a substring test is safe.
FILLER_PHRASES = (
    "अन्य आवश्यक स्रोत",
    "थप आधार र प्रमाण",
    "अन्य प्रमाण",
    "अन्य कागजात",
    "अन्य सबुत",
    "अन्य सवुत",
)

# Matched as WHOLE WORDS -- `आदि` also opens proper nouns, and as a substring it
# rejected `आदित्य …को बैंक खाता विवरण` and `आदिवासी …को लेखापरीक्षण प्रतिवेदन`.
FILLER_TOKENS = ("आदि", "अादी")

# ASCII `.` `:` `;` belong here alongside the danda. Without them `…आदि।` was
# filler and `…आदि.` was not -- the model picks whichever full stop suits the
# script it is already mixing, so punctuation decided whether filler published.
_TOKEN_SPLIT = re.compile(r"[\s,।.:;()\[\]/–—-]+")

# `व`/`ब` is the commonest Nepali orthographic variant (वकपत्र / बकपत्र), and the
# model emits both. Folded before the duplicate check so one document cannot take
# two of the item slots and print twice on the page.
_FOLD = str.maketrans({"ब": "व", "ष": "श", "ऱ": "र"})


def bound_types(case: dict) -> set:
    """Material types bound as evidence. Empty on a LIST payload, which resolves
    no materials -- every check below then falls through rather than guessing."""
    return {mtype for mtype, _, _ in typed_materials(case)}


def has_verdict(case: dict) -> bool:
    """Is a verdict bound? The gate for writing this field at all."""
    return bool(bound_types(case) & set(COURT_TYPES))


def has_charge_sheet(case: dict) -> bool:
    return CHARGE_SHEET_TYPE in bound_types(case)


def has_supreme_reference(case: dict) -> bool:
    """Our only machine-readable signal that an appeal was actually lodged. Its
    absence is what `APPEAL_ITEM` reports -- as "could not be determined", never
    as "no appeal was filed"."""
    return any(SUPREME_REF in (ref or "") for ref in case.get("court_cases") or [])


def held_summary(case: dict) -> str:
    """The bound evidence, for the prompt -- lets the model DIFF instead of guess.

    Nepali labels: the model answers in Nepali, and a mixed-script inventory
    invites it to echo the English type name into public prose.
    """
    labels = {
        "ciaa_press_release": "अख्तियारको प्रेस विज्ञप्ति",
        "press_release": "प्रेस विज्ञप्ति",
        "charge_sheet": "अभियोगपत्र",
        "court_order": "विशेष अदालतको फैसला",
        "news": "समाचार",
    }
    counts = {}
    for mtype, _, _ in typed_materials(case):
        # `material_type` is free-form, not a choices field, so an unlabelled type
        # would render its snake_case English name straight into a Nepali prompt --
        # the very thing this function exists to avoid. Collapse it instead. Such a
        # type has no HELD_DOC_WORDS entry either, so `reject_item` cannot check a
        # claim about it; the collapsed label at least keeps the count honest.
        counts[mtype if mtype in labels else "_other"] = (
            counts.get(mtype if mtype in labels else "_other", 0) + 1)
    if not counts:
        return "(कुनै पनि छैन)"
    labels = {**labels, "_other": "अन्य कागजात"}
    parts = []
    for mtype, n in sorted(counts.items()):
        label = labels[mtype]
        parts.append(f"{label} x{n}" if n > 1 else label)
    return ", ".join(parts)


def floor_items(case: dict) -> list:
    """Deterministic items from what is bound. Verifiable, never model output.

    Empty with no verdict (nothing is written then), and empty when a charge
    sheet AND an appeal reference are both on file -- nothing is certainly absent.
    """
    if not has_verdict(case):
        return []
    items = []
    if not has_charge_sheet(case):
        items.append(CHARGE_SHEET_ITEM)
    if not has_supreme_reference(case):
        items.append(APPEAL_ITEM)
    return items


def render(items) -> str:
    """Join items under the enumerator the corpus uses for that count."""
    items = [t.strip() for t in (items or []) if t and t.strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return "\n".join(f"{NUMERALS[i]}. {t}" for i, t in enumerate(items))
    return "\n".join(f"{LETTERS[i]}) {t}" for i, t in enumerate(items))


def _fold(text: str) -> str:
    """Item text normalised for comparison only -- never for output."""
    return " ".join(text.strip().lower().translate(_FOLD).split())


def reject_item(item: Optional[str], case: dict, accepted=(),
                bound=None, floor=None) -> Optional[str]:
    """Why this model-proposed item must not be used, or None to accept it.

    A REASON STRING rather than a bool so the run log can name the rule that
    fired -- a silently dropped item is indistinguishable from a model that found
    nothing, and those need different follow-up.

    `bound` and `floor` are the case's material types and floor items. They cannot
    change while filtering one reply, so `accept_items` computes them once and
    passes them in; left None they are derived here, which is what a direct caller
    (or a test) wants.
    """
    if not item or not item.strip():
        return "empty"
    item = item.strip()
    bound = bound_types(case) if bound is None else bound
    floor = floor_items(case) if floor is None else floor

    if "<" in item:
        # The field renders through an HTML component, so a tag is interpreted.
        return "contains markup"

    if "\n" in item or "\r" in item:
        # `render` joins items with newlines, so an embedded one leaves a
        # second, UN-enumerated line that closes the frontend's marker list and
        # renders as a stray paragraph.
        return "contains a line break"

    if len(item) > MAX_ITEM_CHARS:
        return f"longer than {MAX_ITEM_CHARS} chars -- a sentence, not a document name"

    if any(w in item for w in FILLER_PHRASES) or _has_filler_token(item):
        return "filler, not a specific document"

    # Already claimed this round, or already stated by the floor. Compared folded,
    # so one document cannot take two slots under two spellings.
    folded = _fold(item)
    for prior in list(floor) + list(accepted):
        if folded == _fold(prior):
            return "duplicate of an item already listed"

    # The two rules below need DIFFERENT exemptions, because they are checking
    # different things:
    #  - the held-verdict rule asks "is this the verdict we hold?", so the escape
    #    is naming ANOTHER COURT -- and it needs a document noun too, since
    #    `सर्वोच्च` alone appears in APPEAL_ITEM itself;
    #  - the appeal rule asks "is this just commentary on whether an appeal
    #    happened?", so the escape is naming a DOCUMENT, with or without a court.
    #    `पुनरावेदन दर्ता किताबको अभिलेख` names no court but is still a record.
    names_a_document = any(w in item for w in COURT_DOC_WORDS)
    names_other_court = names_a_document and any(w in item for w in OTHER_COURT_WORDS)

    # HEAD-matched, not substring-matched, for the same reason the held-document
    # rule is: `अभियोगपत्रमा उल्लेखित संलग्न अनुसूची` is an annex the charge sheet
    # REFERENCES. A substring test dropped it on every case with no charge sheet
    # bound -- 24 of the first batch's 25 -- which is the module's own canonical
    # keeper. The appeal rule below stays a substring test on purpose: it catches
    # COMMENTARY about the appeal, which has no document head to match.
    if CHARGE_SHEET_ITEM in floor and any(_names_held_document(item, w)
                                          for w in CHARGE_SHEET_WORDS):
        return "restates the charge-sheet item"
    if (APPEAL_ITEM in floor and not names_a_document
            and any(w in item for w in APPEAL_WORDS)):
        return "restates the appeal item"

    # The model was shown what we hold, so a claim that a held document is
    # missing is contradicted by our own bindings.
    for mtype in bound:
        if mtype in COURT_TYPES and names_other_court:
            continue
        for word in HELD_DOC_WORDS.get(mtype, ()):
            if _names_held_document(item, word):
                return f"claims a {mtype} is missing, but one is bound as evidence"

    return None


def _has_filler_token(item: str) -> bool:
    """Does `item` use a filler word as a WHOLE word? See `FILLER_TOKENS`."""
    return any(tok.strip("।,") in FILLER_TOKENS for tok in _TOKEN_SPLIT.split(item))


def _names_held_document(item: str, word: str) -> bool:
    """Is the held document this phrase's HEAD, or only a modifier inside it?

    `अभियोगपत्रमा उल्लेखित संलग्न अनुसूची` -- an annex the charge sheet references --
    contains `अभियोगपत्र` but its head is `अनुसूची`, a different document, and
    dropping it would throw away exactly the specific finding this output is for.
    Nepali noun phrases are head-final, so the head is the last noun once trailing
    parentheticals, copy-of nouns and case endings are peeled off.

    This replaced a `len(word) / len(item) >= 0.5` ratio, which was backwards. The
    prompt demands specificity -- "name the document, with its date, party, phase,
    or number" -- and every qualifier lengthens the item and pushes the ratio down,
    so the rule stopped firing precisely on the items it was written for:
    `विशेष अदालत काठमाडौंको फैसला (०८१-CR-००९१)` passed at 0.13 while the bare
    `विशेष अदालतको फैसला` was caught. The more specific the wrong claim, the more
    likely it published.
    """
    return word in item and _head(item).endswith(word)


# Nouns meaning "a copy of the preceding document", so the head is what came
# before them. NOT generic words like `कागजात` -- those are heads in their own right.
COPY_NOUNS = ("प्रतिलिपि", "पूर्णपाठ", "पूर्ण पाठ", "पुर्णपाठ", "पुर्ण पाठ",
              "मूलपाठ", "मूल पाठ", "सक्कल", "फोटोकपी", "प्रति")

# Case endings that sit between a modifier and its head.
CASE_ENDINGS = ("को", "का", "की", "मा", "ले", "बाट", "लाई", "सँग", "उपर")

_PARENTHETICAL = re.compile(r"[(（][^)）]*[)）]")


def _head(item: str) -> str:
    """The phrase with trailing qualifiers peeled off, so it ends on its head noun."""
    text = _PARENTHETICAL.sub("", item).strip(" ।,.-")
    changed = True
    while changed:
        changed = False
        for tail in COPY_NOUNS + CASE_ENDINGS:
            if text.endswith(tail) and len(text) > len(tail):
                text = text[: -len(tail)].strip(" ।,.-")
                changed = True
    return text


def accept_items(proposed, case: dict, max_items: int = MAX_LLM_ITEMS):
    """Filter model items to the keepers. Returns `(accepted, [(item, reason)])`.

    Rejections are returned, not logged, to keep this module pure.
    """
    accepted, rejected = [], []
    # Derived ONCE -- neither can change while filtering one reply, and
    # `reject_item` used to re-walk the evidence list three times per item.
    bound, floor = bound_types(case), floor_items(case)
    for item in list(proposed or []):
        if not isinstance(item, str):
            rejected.append((repr(item), "not a string"))
            continue
        if len(accepted) >= max_items:
            rejected.append((item.strip(), f"over the {max_items}-item cap"))
            continue
        reason = reject_item(item, case, accepted=accepted, bound=bound, floor=floor)
        if reason:
            rejected.append((item.strip(), reason))
            continue
        accepted.append(item.strip())
    return accepted, rejected


def build(case: dict, items=()) -> Optional[str]:
    """Final `missing_details` value for `case`, or None to write nothing.

    `items` is ALREADY-ACCEPTED model output (see `accept_items`). None is a real
    outcome, not a failure -- with no verdict, or with both floor items satisfied
    and nothing found, there is nothing honest to say, and the frontend hides the
    section on a blank value.

    Over `MAX_CHARS`, trailing items are DROPPED whole rather than truncated
    mid-phrase: a half-written document name is worse than an absent one. That
    trim is a SANITY GUARD, not a policy -- at 299 and again at 450 it cut real
    findings, always the last and most specific one, because specificity is long.
    A full 2-floor + `MAX_LLM_ITEMS` value cannot reach 700, so the ITEM COUNT is
    the binding limit.
    """
    if not has_verdict(case):
        return None
    chosen = floor_items(case) + [t.strip() for t in (items or []) if t and t.strip()]
    chosen = chosen[:MAX_ITEMS]
    while chosen and len(render(chosen)) > MAX_CHARS:
        chosen.pop()
    return render(chosen) or None
