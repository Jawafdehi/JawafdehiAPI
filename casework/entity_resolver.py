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
