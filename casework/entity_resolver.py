"""Resolve an LLM-extracted Nepali name to a canonical NES entity id.

PURE: no I/O, no Django, no LLM. The caller fetches candidates (see
`CaseworkApi.search_entities`) and this module decides. That split is what makes
the decision deterministic and testable offline — the same name against the same
candidate list always decides the same way.

THE GOVERNING CONSTRAINT: a wrong bind publicly attaches a named individual to a
corruption case they had nothing to do with. A missed bind costs a caseworker
five minutes. Every tradeoff here resolves in that direction, which is why there
is no edit distance anywhere in this file — it is the only mechanism that would
let श्रेष्ठ match श्रेष्ट, and a one-character difference must never bind.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from jawafdehi_shared.entities.ids import is_valid_entity_iri
from jawafdehi_shared.search.transliterate import to_roman_colloquial

# Honorifics and titles carry no identity. Stripped before tokens are compared so
# "श्री अनिष श्रेष्ठ" and "अनिष श्रेष्ठ" are the same name.
HONORIFICS = frozenset({
    "श्री", "श्रीमती", "सुश्री", "डा", "डा.", "डाक्टर", "इन्जिनियर", "इन्ज", "इन्ज.",
    "प्रा", "प्रा.", "dr", "dr.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.",
    "engineer", "er", "er.", "prof", "prof.",
})

# Nepali middle particles. An interior token missing from one side of a
# comparison is forgiven ONLY if it is one of these. Anything else means the two
# strings may name different people — that is a review, not a bind. Without this
# rule the resolver bound "विजय शाह" to person/bija-bikram-shaha-178948, because
# बिक्रम looked like a droppable middle name.
MIDDLE_PARTICLES = frozenset({
    "बहादुर", "कुमार", "कुमारी", "प्रसाद", "देवी", "लाल", "नारायण", "माया", "राज",
    "bahadur", "bahadura", "kumar", "kumara", "kumari", "prasad", "prasada",
    "devi", "lal", "lala", "narayan", "narayana", "maya", "raj", "raja",
})

# Institutional words that identify nobody on their own. A name made only of
# these goes to review: bare "जिल्ला वन कार्यालय" matches a generic office entity
# while the case's office is the one in Mugu.
GENERIC_TOKENS = frozenset({
    "कार्यालय", "समिति", "विभाग", "मन्त्रालय", "शाखा", "इकाई", "केन्द्र",
    "उपभोक्ता", "जिल्ला", "गाउँपालिका", "नगरपालिका", "प्रदेश", "आयोजना", "निर्माण",
    # "वन" (forest): the missing third token of this constant's own worked
    # example, "जिल्ला वन कार्यालय" -- a bare District Forest Office name is
    # still generic (there is one per district) even though "forest" is a
    # domain word rather than an organisational-structure word like the rest
    # of this set. Added in Task 3 so the genericity veto actually catches
    # the case the docstring above describes.
    "वन",
})

# Nepali family names, frequency-ranked from real data: the 686 accused binds on
# published Jawafdehi cases plus the 138 extracted names in the resolver's
# labelled set. Used ONLY to decide whether a two-token name may be read
# surname-first. Curated rather than inferred because "is this token a surname"
# has no algorithmic answer -- राम is a given name, राई is a surname, and no rule
# separates them.
SURNAMES = frozenset({
    "श्रेष्ठ", "shrestha", "साह", "sah", "saha", "यादव", "yadav", "yadava",
    "मिश्र", "mishra", "पौडेल", "poudel", "paudel", "थापा", "thapa",
    "राई", "rai", "गिरी", "giri", "भट्टराई", "bhattarai", "धिमाल", "dhimal",
    "लिम्बू", "limbu", "अधिकारी", "adhikari", "मण्डल", "mandal", "मंडल",
    "बस्नेत", "basnet", "महर्जन", "maharjan", "उपाध्याय", "upadhyay",
    "नेपाल", "nepal", "पुरी", "puri", "शर्मा", "sharma", "sharmaa",
    "महतो", "mahto", "कोइराला", "koirala", "चौधरी", "chaudhari", "chaudhary",
    "शाही", "shahi", "कार्की", "karki", "भट्ट", "bhatt", "bhatta",
    "जोशी", "joshi", "शाह", "shah", "shaha", "पोखरेल", "pokharel", "pokhrel",
    "आचार्य", "acharya", "वली", "wali", "vali", "झा", "jha", "दास", "das",
    "माझी", "majhi", "दर्जी", "darji", "ठाकुर", "thakur", "अर्याल", "aryal",
    "रावल", "rawal", "ravala", "सिंह", "singh", "खत्री", "khatri",
    "मगर", "magar", "भण्डारी", "bhandari", "ढकाल", "dhakal",
    "उप्रेती", "upreti", "चौलागाईं", "chaulagain", "पाण्डे", "pandey",
    "घिमिरे", "ghimire", "तिम्सिना", "timsina", "रानाभाट", "ranabhat",
    "शेर्पा", "sherpa", "तामाङ", "tamang", "गुरुङ", "gurung",
    "बिष्ट", "bista", "bishta", "खड्का", "khadka", "कुमाल", "kumal",
    "सुनार", "sunar", "नेपाली", "nepali", "गौतम", "gautam",
    "रेग्मी", "regmi", "सुवेदी", "subedi", "कलवार", "kalwar",
    "खत्वे", "khatve", "कमली", "kamali", "प्रसाई", "prasai",
    "ज्ञवाली", "gyawali", "पराजुली", "parajuli", "बुढा", "budha",
    "मुखिया", "mukhiya", "बम", "bam", "प्याकुरेल", "pyakurel", "खनाल", "khanal",
})

# Latin spellings the colloquial fold does not reach. to_roman_colloquial gives
# श्रेष्ठ -> "shreshtha", but people type "Shrestha", so the two only meet through
# a curated table. Curated and not algorithmic on purpose: an algorithm that
# collapsed "sht" to "st" would also collapse genuinely different names.
LATIN_VARIANTS = (
    frozenset({"shrestha", "shreshtha", "shreshth", "srestha"}),
    frozenset({"poudel", "paudel", "paudyal", "paudela"}),
    frozenset({"bista", "bishta", "bisht"}),
    frozenset({"basnet", "basnyat", "basnett"}),
    frozenset({"chaudhary", "chaudhari", "chaudhry", "choudhary"}),
    frozenset({"adhikari", "adhikary"}),
    frozenset({"karki", "karky"}),
    frozenset({"thapa", "thapaa"}),
    frozenset({"maharjan", "maharjun"}),
    frozenset({"gyawali", "gyanwali", "jnawali"}),
)

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"))
# A stray visarga or colon lands on the end of extracted names ("टिकाराम ज्ञवालीः").
_TRAILING_MARKS = re.compile(r"[ः:।\.]+\s*$")
_PUNCT = re.compile(r"[^\wऀ-ॿ\s-]")
_WHITESPACE = re.compile(r"\s+")


def normalise_name(raw: str) -> str:
    """NFC, zero-width stripped, punctuation to spaces, whitespace collapsed,
    lowercased. Hyphens SURVIVE — the composite-name veto looks for " - "."""
    text = unicodedata.normalize("NFC", raw or "").translate(_ZERO_WIDTH)
    text = _TRAILING_MARKS.sub("", text.strip())
    text = _PUNCT.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip().lower()


def token_forms(token: str) -> frozenset[str]:
    """Every spelling of one token that counts as the same token.

    The token itself, plus its colloquial romanisations when it is non-ASCII
    (to_roman_colloquial emits both the schwa-kept and schwa-dropped spellings),
    plus any curated Latin variant group it belongs to.
    """
    forms = {token}
    if not token.isascii():
        forms.update(to_roman_colloquial(token).split())
    for group in LATIN_VARIANTS:
        if forms & group:
            forms |= group
    return frozenset(forms)


def name_tokens(raw: str) -> tuple[tuple[str, frozenset[str]], ...]:
    """`(normalised_token, its form-set)` pairs, honorifics dropped.

    The raw token is kept alongside the forms so the scorer can tell an identity
    match from a match that only holds through romanisation.
    """
    out = []
    for token in normalise_name(raw).replace("-", " ").split():
        if token in HONORIFICS:
            continue
        out.append((token, token_forms(token)))
    return tuple(out)


def tokens_equal(a, b) -> bool:
    """True when two `(raw, forms)` tokens name the same thing."""
    return bool(a[1] & b[1])


# The single bind threshold. Set by the worked table below, NOT by how many binds
# it produces:
#     identical after normalisation                        1.00  bind
#     four tokens bridged across scripts                   0.92  bind
#     two tokens, one omitted particle                     0.95  bind
#     four bridged tokens, one omitted particle            0.87  bind
#     two tokens, two omitted particles                    0.80  review
#     four bridged tokens, two omitted particles           0.72  review
#     anchor mismatch, or a non-particle omission          0.00  no match
# Two stacked guesses is not near-certain, so it reports. Moving this constant
# moves a name between buckets and changes nothing else about the output.
MIN_BIND_SCORE = 0.85
# A token that matches only through romanisation or the curated variant table is
# weaker evidence than an identical token, but not much weaker.
VARIANT_PENALTY = 0.02
# A missing middle particle is the one omission that does not sink a match.
PARTICLE_PENALTY = 0.05
# The second and later omissions cost more. Dropping one middle particle is a
# spelling habit; dropping two means the strings may name different people. With
# a flat penalty "राम थापा" would bind to "राम बहादुर प्रसाद थापा" at 0.90.
EXTRA_OMISSION_PENALTY = 0.10


def _is_particle(token) -> bool:
    return bool(token[1] & MIDDLE_PARTICLES)


def _is_surname(token) -> bool:
    return bool(token[1] & SURNAMES)


def _anchors_match(left, right) -> bool:
    """Both names' first-and-last token pair must match, order notwithstanding.

    "Order-insensitive" has to cover a fully reordered two-token name
    ("Shrestha Anish" vs "अनिष श्रेष्ठ", surname-first against given-name-first),
    where left[0] pairs with right[-1] rather than right[0]. So this checks the
    *unordered* pair {left[0], left[-1]} against {right[0], right[-1]}: the
    straight correspondence always counts.

    The swapped correspondence does NOT always count. Without a further check,
    a two-token name where both tokens can be either end -- "कृष्ण राम" vs
    "राम कृष्ण", both plain given names -- would score a perfect 1.0, identical
    to an exact match, on nothing but a coincidental permutation. No penalty
    fixes this: any deduction big enough to sink "कृष्ण राम"/"राम कृष्ण" also
    sinks "Shrestha Anish"/"अनिष श्रेष्ठ", which scores lower (0.96) precisely
    because it crosses scripts. So a swap is only accepted when exactly one
    anchor is a curated SURNAME -- that token pins which end is the surname,
    so the reorder is verifiable rather than guessed. If neither anchor is a
    known surname, or both are ("थापा मगर" vs "मगर थापा" -- which one is the
    surname is unknowable), the swap fails closed.
    """
    first_l, last_l = left[0], left[-1]
    first_r, last_r = right[0], right[-1]
    if tokens_equal(first_l, first_r) and tokens_equal(last_l, last_r):
        return True
    if not (tokens_equal(first_l, last_r) and tokens_equal(last_l, first_r)):
        return False
    return _is_surname(first_l) != _is_surname(last_l)


def match_score(extracted: str, candidate: str) -> float:
    """How near-certain it is that these two strings name the same entity.

    1.0 is an exact match after normalisation; 0.0 means "do not bind". Between
    them the only deductions are VARIANT_PENALTY per token matched through
    romanisation, PARTICLE_PENALTY per omitted middle particle, and
    EXTRA_OMISSION_PENALTY for every omission after the first.

    Order-insensitive for a reordered given-name/surname pair, but only when
    the reorder is verifiable -- see `_anchors_match`: a straight anchor match
    always counts, a swapped one only when exactly one anchor is a curated
    SURNAME. Both ANCHORS -- first and last token -- must match one of those
    two ways, which is what stops a partial match: extracted
    "घुरनी देवी खत्वे" against the stored "घुरनी देवी" scores 0.
    """
    left, right = name_tokens(extracted), name_tokens(candidate)
    if not left or not right:
        return 0.0
    if not _anchors_match(left, right):
        return 0.0

    longer, shorter = (left, right) if len(left) >= len(right) else (right, left)
    # Order-insensitive matching: pull each shorter token out of a mutable pool
    # of longer tokens (by content, not position) rather than walking both in
    # lockstep. A positional walk would mis-score a fully reordered name: it
    # would consume the wrong longer token as "omitted" before ever reaching
    # the one that actually pairs with the current shorter token.
    pool = list(longer)
    variants = 0
    for token in shorter:
        match_at = next(
            (i for i, candidate_token in enumerate(pool) if tokens_equal(token, candidate_token)),
            None,
        )
        if match_at is None:
            # A token of the shorter name pairs with nothing. Not a match.
            return 0.0
        matched = pool.pop(match_at)
        if token[0] != matched[0]:
            # Greedy: the first pool token that satisfies tokens_equal is taken,
            # even if a later still-unmatched one would have been an identity
            # match rather than a variant. That can only over-count variants,
            # never under-count them, so it only ever fails toward a LOWER
            # score -- never toward a wrongful bind.
            variants += 1

    # Whatever is left in the pool once every shorter token has claimed one is
    # what the longer name has that the shorter one doesn't: the omissions.
    omitted = pool
    if any(not _is_particle(token) for token in omitted):
        return 0.0
    dropped = len(omitted)
    return max(
        0.0,
        1.0
        - VARIANT_PENALTY * variants
        - PARTICLE_PENALTY * dropped
        - EXTRA_OMISSION_PENALTY * max(0, dropped - 1),
    )


BIND = "BIND"
REVIEW = "REVIEW"
NO_MATCH = "NO_MATCH"

# The separator the extractor uses for the location shape "Activity - Location"
# ("जिल्ला वन कार्यालय - मुगु जिल्ला"). Splitting it would bind the WRONG district's
# office: bare "जिल्ला वन कार्यालय" matches a generic office entity. Never split.
_COMPOSITE_SEPARATOR = " - "
# A trailing id segment on an NES slug (person/khusilala-saha-865cdc): a hex
# suffix or a plain number, never part of the name.
_SLUG_ID_SUFFIX = re.compile(r"-(?:[0-9a-f]{4,8}|\d+)$")


@dataclass(frozen=True)
class Decision:
    """What to do with one extracted name. `candidates` carries every scoring
    candidate so the review file can reproduce the decision without re-querying.
    """

    verdict: str
    nes_id: str | None
    score: float
    matched_name: str
    reason: str
    candidates: tuple

    @property
    def is_bind(self) -> bool:
        return self.verdict == BIND


def candidate_name_forms(result: dict) -> tuple[str, ...]:
    """The name strings of one search result worth scoring against.

    The Devanagari title, the English title, and the IRI slug. The slug is NES's
    own romanisation of the name, so it matches Latin extractions that neither
    title reaches; its trailing id segment is dropped first.
    """
    title = result.get("title") or {}
    forms = [title.get("ne"), title.get("en")]
    slug = (result.get("id") or "").rsplit("/", 1)[-1]
    if slug:
        forms.append(_SLUG_ID_SUFFIX.sub("", slug).replace("-", " "))
    return tuple(form for form in forms if form and form.strip())


def _name_vetoes(extracted: str) -> str:
    """The reason this name can never be auto-bound, or "" if none applies.

    These are properties of the extracted string alone, independent of what NES
    holds.
    """
    normalised = normalise_name(extracted)
    if _COMPOSITE_SEPARATOR in normalised:
        return "composite 'Activity - Location' name, never split"
    tokens = name_tokens(extracted)
    if len(tokens) < 2:
        return "single token is too weak an anchor"
    if all(token[1] & GENERIC_TOKENS for token in tokens):
        return "generic institutional name identifies no specific entity"
    return ""


def resolve(extracted_name: str, candidates) -> Decision:
    """Decide what to do with one LLM-extracted name.

    BIND only when exactly ONE NES entity scores at or above MIN_BIND_SCORE and
    no veto applies. More than one qualifying entity is an ambiguity and goes to
    review -- 12 of the 138 real extracted strings hit that, with up to 13
    same-name entities for "संजय प्रसाद यादव".

    A candidate whose @id is not a canonical entity IRI is dropped before
    scoring, so a malformed IRI can never reach the API.
    """
    scored = []
    for result in candidates or ():
        nes_id = (result.get("id") or "").strip()
        if not is_valid_entity_iri(nes_id):
            continue
        best = max(
            ((match_score(extracted_name, form), form)
             for form in candidate_name_forms(result)),
            default=(0.0, ""),
        )
        if best[0] > 0:
            scored.append((best[0], nes_id, best[1]))
    scored.sort(key=lambda row: (-row[0], row[1]))
    frozen = tuple(scored)

    qualifying = {nes_id for score, nes_id, _ in scored if score >= MIN_BIND_SCORE}
    if not qualifying:
        return Decision(NO_MATCH, None, scored[0][0] if scored else 0.0,
                        scored[0][2] if scored else "",
                        "no NES entity scored at or above the bind threshold",
                        frozen)
    if len(qualifying) > 1:
        return Decision(REVIEW, None, scored[0][0], scored[0][2],
                        f"ambiguous: {len(qualifying)} distinct NES entities score "
                        f"at or above the bind threshold", frozen)
    veto = _name_vetoes(extracted_name)
    if veto:
        return Decision(REVIEW, None, scored[0][0], scored[0][2], veto, frozen)
    score, nes_id, matched = scored[0]
    return Decision(BIND, nes_id, score, matched, "", frozen)
