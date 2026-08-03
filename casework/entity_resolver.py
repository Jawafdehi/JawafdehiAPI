"""Resolve an LLM-extracted Nepali name to a canonical NES entity id.

PURE: no I/O, no Django, no LLM. The caller fetches candidates (see
`CaseworkApi.search_entities`) and this module decides. That split is what makes
the decision deterministic and testable offline — the same name against the same
candidate list always decides the same way.

THE GOVERNING CONSTRAINT: a wrong bind publicly attaches a named individual to a
corruption case they had nothing to do with. A missed bind costs a caseworker
five minutes. Every tradeoff here resolves in that direction, which is why no
similarity ratio or edit-distance ALGORITHM appears anywhere in this file: no
Levenshtein, no SequenceMatcher, no fuzzy threshold. Two tokens match only by
being equal in some spelling, never by being nearly equal. So a consonant
difference cannot bind — श्रेष्ठ against श्रेष्ट scores 0.0.

The one exception, measured rather than assumed: `to_roman_colloquial` folds
Devanagari VOWEL LENGTH, so ल्हमु and ल्हामु both romanise to "lhamu" and
match_score("मिङमा ल्हमु शेर्पा", "मिङमा ल्हामु शेर्पा") is 0.98 — a bind on a
one-character difference, by a fold rather than by an algorithm. That is
deliberate and kept: the same fold is what lets निधि meet the stored निधी, a real
variant a caseworker would want bound, and `to_roman_colloquial` is the shared
platform romanisation four indexers depend on.

Read the next sentence before you touch the veto. Across the 142-name labelled
set the fold caused no false positive THAT SURVIVED THE DOCUMENT VETO — not none
at all. Its one effect beyond निधि/निधी was मिङमा ल्हमु शेर्पा matching
person/mingna-lhamu-sherpa-328030 at 0.98, an Election Commission namesake that
`resolve` alone binds and only `apply_document_veto` suppresses. So this fold's
safety record is borrowed from that veto, and weakening the veto re-opens it.
`test_entity_resolver.py` asserts the behaviour so it stays a known property
instead of a surprise. Vowel length is the ONLY difference that folds this way.
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

# Organisational-FORM words: the words that say what KIND of body a name names
# (office, committee, ministry, municipality) rather than which one. Two jobs:
#
#   1. The gate on the structural unqualified-institution veto below. A name
#      carrying one of these words names an institution, so
#      `_unqualified_institution_veto` may test it for the bucket shape; a name
#      carrying none is a person or a company and is left alone.
#   2. A backstop: a name made ONLY of these identifies nobody at all, so
#      `_name_vetoes` reviews it outright without needing a candidate list.
#
# This set deliberately does NOT try to enumerate the DOMAIN words that make an
# office generic (मालपोत land-revenue, नापी survey, राजस्व revenue, प्रहरी police,
# स्वास्थ्य health, ...). That list has no end, and the veto that needed it was a
# whack-a-mole: `जिल्ला वन कार्यालय` was fixed by adding वन and the neighbouring
# `मालपोत कार्यालय` walked through unchanged. The structural veto below replaced
# that mechanism, so this set only has to stay closed over organisational FORMS,
# which it can be -- Nepal has a fixed inventory of them.
#
# Both scripts, like MIDDLE_PARTICLES and SURNAMES: token_forms only adds a
# romanisation for a NON-ASCII extracted token, so a Latin extraction like
# "District Forest Office" or "Jilla Van Karyalaya" (the extractor emits both
# fully-English and bilingual-transliterated institutional names) can only
# intersect this set if the English/Latin spelling is a literal member too.
# Devanagari + the exact to_roman_colloquial output (both the schwa-kept and
# schwa-dropped spelling, verified per word) for the words already here, plus
# the plain English institutional words a bilingual extraction would use.
GENERIC_TOKENS = frozenset({
    "कार्यालय", "karyalaya", "karyalay", "office",
    "समिति", "samiti", "committee",
    "विभाग", "vibhaga", "vibhag", "department",
    "मन्त्रालय", "mantralaya", "mantralay", "ministry",
    "शाखा", "shakha", "branch",
    "इकाई", "ikai", "unit",
    "केन्द्र", "kendra", "kendr", "centre", "center",
    "उपभोक्ता", "upabhokta", "consumer",
    "जिल्ला", "jilla", "district",
    # गाउँपालिका's algorithmic romanisation ("gau~palika") is not a spelling
    # anyone types; "gaunpalika" is the practical ASCII form.
    "गाउँपालिका", "gaunpalika",
    "नगरपालिका", "nagarapalika", "nagarpalika", "municipality",
    "प्रदेश", "pradesha", "pradesh", "province",
    "आयोजना", "ayojana", "project",
    "निर्माण", "nirmana", "nirman", "construction",
    # "वन" (forest) is the one DOMAIN word here, added in Task 3 to make the
    # all-generic backstop catch "जिल्ला वन कार्यालय". The structural veto below
    # now vetoes that row on its own (measured: with वन removed from this set,
    # `_unqualified_institution_veto` still holds it, on the sibling
    # "जिल्ला वन कार्यालय कपिलवस्तु" in its own candidate list). It stays as
    # redundant cover rather than as the mechanism, because that row would
    # otherwise rest on a single sibling being inside the search window. It is
    # NOT an invitation to add the next domain word -- add nothing here.
    "वन", "vana", "van", "forest",
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
# A trailing id segment on an NES slug (person/khusilala-saha-865cdc): 6-8
# lowercase hex characters with at least one digit -- every real id suffix in
# the prod-verified fixtures is exactly this shape (219986, 285096, 2de9b3,
# f4548e, 865cdc, 11aa22). Narrower than a bare "[0-9a-f]{4,8}|\d+", which also
# strips a genuine trailing digit that distinguishes two entities ("...aayojana
# 2" -> "...aayojana", collapsing project 2 into project 1) or an all-letters
# name segment that happens to fall in [0-9a-f] ("...baba", 4 hex-range
# letters, no digit -- not an id).
_SLUG_ID_SUFFIX = re.compile(r"-(?=[0-9a-f]{6,8}$)(?=[0-9a-f]*\d)[0-9a-f]{6,8}$")

# Nepal's seven provinces, keyed by the slug NES puts in a provincial IRI:
# organization/government/provincial/<slug>/<body>.
#
# WHY: many provincial bodies' stored TITLES do not mention their province, and
# prod holds exactly ONE entity titled "वन तथा वातावरण मन्त्रालय" -- Gandaki's
# (verified 2026-08-03: 305 search hits, one exact title.ne match). So a DRAFT
# district-forest case in Bara, which is in Madhesh province, extracted that bare
# ministry name, matched at 1.00, and the ambiguity veto could not fire because
# the name IS unique in NES. The IRI quietly asserts a province the name never
# claimed: unique in NES, not unique in reality. Uniqueness is the whole argument
# -- where a bare title IS duplicated the ambiguity veto already holds it.
#
# DERIVED FROM PROD, not invented (2026-08-03, GET only):
#   1. Swept /api/search/?type=entity over nine ministry/province seeds and
#      collected every distinct `/provincial/<slug>/` segment -> exactly these 7.
#   2. Followed each provincial ministry's `containedInPlace` to its province
#      entity (location/province/<slug>-np0N) and read `name.ne`.
# Both steps are reproducible; the sweep output is in the fix-round-4 report.
#
# The Devanagari spellings come from those province entities, PLUS curated
# variants, because two of them cannot be taken from prod as-is:
#   * koshi's province entity is still titled "प्रदेश १" / "Province No. 1" -- the
#     pre-rename name -- so "कोशी" is curated, with "कोसी" as the common variant.
#   * bagmati stores "वाग्मती" but almost everyone writes "बागमती". Both accepted.
# The Latin forms include the IRI slug itself because to_roman_colloquial does NOT
# reproduce it: बागमती folds to "bagamati" not "bagmati", and सुदूरपश्चिम to
# "sudurapashcim" not "sudurpashchim". Matching the slug against the fold is the
# approach that fails, which is why the accepted spellings are listed instead.
# THE RULE FOR THIS TABLE, because every extra spelling can only OPEN the
# allow-path and the governing constraint says be stingy in that direction. Each
# province gets exactly:
#   (a) the Devanagari spelling prod stores, from its province entity's name.ne;
#   (b) one Devanagari variant, ONLY where prod's spelling is not the one people
#       write -- stated per entry below, never assumed;
#   (c) the IRI slug, as the Latin form, because that is what a Latin extraction
#       actually contains and the fold does not reproduce it.
# Romanisations of (a)/(b) are deliberately ABSENT: they are redundant. An
# extracted Devanagari token carries its own raw form into `token_forms`, so the
# Devanagari entry already matches it -- adding the fold widens nothing.
# `test_province_forms_carry_no_redundant_romanisation` enforces that.
PROVINCE_NAME_FORMS = {
    # कोशी is curated: the province entity still stores its pre-rename name
    # "प्रदेश १" / "Province No. 1", so prod has no usable spelling. कोसी is the
    # common variant (श vs स) seen in Nepali prose.
    "koshi": frozenset({"कोशी", "कोसी", "koshi"}),
    # मधेस is the common variant of prod's मधेश, same श/स alternation.
    "madhesh": frozenset({"मधेश", "मधेस", "madhesh"}),
    # prod stores वाग्मती; बागमती is what almost everyone writes.
    "bagmati": frozenset({"बागमती", "वाग्मती", "bagmati"}),
    # गंडकी is the anusvara spelling of prod's गण्डकी.
    "gandaki": frozenset({"गण्डकी", "गंडकी", "gandaki"}),
    "lumbini": frozenset({"लुम्बिनी", "lumbini"}),
    "karnali": frozenset({"कर्णाली", "karnali"}),
    # सुदुरपश्चिम drops the ū of prod's सुदूरपश्चिम, a routine typing variant.
    "sudurpashchim": frozenset({"सुदूरपश्चिम", "सुदुरपश्चिम", "sudurpashchim"}),
}
# The IRI segment that asserts a province.
_PROVINCIAL_SEGMENT = re.compile(r"/provincial/([^/]+)/")


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
    candidates: tuple[tuple[float, str, str], ...]

    @property
    def is_bind(self) -> bool:
        return self.verdict == BIND


def candidate_name_forms(result: dict) -> tuple[str, ...]:
    """The name strings of one search result worth scoring against.

    The Devanagari title, the English title, and the IRI slug with its trailing
    id segment dropped.

    THE SLUG FORM SCORES BUT NEVER DECIDES. Measured over all 7,882 candidate
    rows in `tests/casework/fixtures/entity_candidates.json`: it scores above 0
    on 53 rows and strictly beats both titles on ZERO of them, so removing it
    changes no verdict on the labelled set. It does not reach Latin extractions
    the titles miss -- `token_forms` already romanises Devanagari tokens on both
    sides of the comparison, so a Latin extraction reaches `title.ne` directly.
    It is kept because the three forms enter a `max()`, where a redundant form
    costs nothing; it is not a safety net, since `max()` can only ever RAISE a
    candidate's score.
    """
    title = result.get("title") or {}
    forms = [title.get("ne"), title.get("en")]
    slug = (result.get("id") or "").rsplit("/", 1)[-1]
    if slug:
        forms.append(_SLUG_ID_SUFFIX.sub("", slug).replace("-", " "))
    return tuple(form for form in forms if form and form.strip())


def asserted_province(nes_id: str) -> str:
    """The province slug a candidate IRI asserts, or "" when it asserts none.

    Returns the slug WHATEVER it is, including one absent from
    `PROVINCE_NAME_FORMS`. An unrecognised slug is a province we cannot verify,
    not a province that is absent -- `_province_veto` fails closed on it. Nepal
    has seven provinces and all seven are curated, so an unknown slug means NES
    changed and a human should look.
    """
    match = _PROVINCIAL_SEGMENT.search(nes_id or "")
    return match.group(1).lower() if match else ""


def names_the_province(extracted: str, province: str) -> bool:
    """True when the extracted name carries `province`'s name, in either script.

    Uses the same token machinery as every other comparison here, so a Latin
    extraction reaches the Devanagari spellings through `token_forms`.
    """
    accepted = PROVINCE_NAME_FORMS.get(province, frozenset())
    return any(forms & accepted for _raw, forms in name_tokens(extracted))


def _province_veto(extracted: str, nes_id: str) -> str:
    """Reason this candidate's asserted province is unconfirmed, or "".

    The twin of the election-record problem, and it needs no HTTP call: both the
    extracted name and the candidate IRI are already in hand, so this is a pure
    name/candidate veto in the same family as the ambiguity and genericity vetoes.

    When a provincial body's stored title does not name its province, binding on
    the title alone accepts whichever province NES happens to hold. That is how
    `वन तथा वातावरण मन्त्रालय` bound at 1.00 to
    `organization/government/provincial/gandaki/mofesc` for a DRAFT
    district-forest case in Bara, which is in Madhesh province. Found by a smoke
    run over unseen DRAFT cases, not by review. The case identifiers stay out of
    git; the evidence is in
    `work/2026-08-03-Fix-related_entities-enricher/`.

    Note how thin the alternative protection is: `स्वास्थ्य तथा जनसंख्या मन्त्रालय`
    already reviews, but only because NES happens to hold two entities with that
    title. Relying on duplicate count means whether we are protected is an
    accident of the data.
    """
    province = asserted_province(nes_id)
    if not province:
        return ""
    if province not in PROVINCE_NAME_FORMS:
        # Fails closed, like the unreadable-document branch of the document veto:
        # a province slug we have no spellings for is one we cannot check.
        return (
            f"candidate is scoped to an unrecognised province {province!r} "
            f"({nes_id}); add its spellings to PROVINCE_NAME_FORMS before this can "
            "bind"
        )
    if names_the_province(extracted, province):
        return ""
    return (
        f"candidate is scoped to {province} province ({nes_id}) but the extracted "
        f"name does not say which province. Every province has a body with this "
        f"name and only one of them is in NES, so confirm the case is in "
        f"{province} before binding."
    )


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


def names_an_institution(extracted: str) -> bool:
    """True when the name carries an organisational-FORM word.

    The gate on the veto below: "office", "committee", "ministry",
    "municipality", "department" and the rest of GENERIC_TOKENS say the name
    designates a body rather than a person or a company. A person name never
    carries one, so persons never reach the bucket test -- which is what keeps
    that test off the shape it would misread (a two-token given-name/surname pair
    that happens to be the leading half of a longer person's name).
    """
    return any(token[1] & GENERIC_TOKENS for token in name_tokens(extracted))


def qualified_siblings(extracted: str, candidates: list[dict],
                       exclude: str = "") -> tuple[str, ...]:
    """Candidate titles that are the extracted name PLUS a trailing qualifier.

    Positional and strict: every token of `extracted` must equal the token at the
    SAME INDEX of the candidate's title, and the title must carry at least one
    more token after them. So the bare `मालपोत कार्यालय` has
    `मालपोत कार्यालय, पर्सा` and `मालपोत कार्यालय सुर्खेत` as qualified siblings --
    each is the same name with a district appended.

    Prefix, not subset, and that asymmetry is the whole discriminator. Nepali
    institution names APPEND their locality, so:

      * a name that is a strict PREFIX of other NES titles is the unqualified
        head of a family of offices -- one per district, and NES holds the
        district-qualified members;
      * a name other NES titles END with is a PLACE that owns them --
        `अदानचुली गाउँपालिका` is the tail of
        `वडा नं. 1 को कार्यालय, अदानचुली गाउँपालिका`, which is a ward office
        INSIDE that municipality, a child of the entity rather than another
        instance of it.

    A subset test cannot tell those apart and would refuse all seven
    municipality binds in `tests/casework/fixtures/` on top of this one --
    measured: 33 correct binds down to 26, recall 0.846 -> 0.667. The prefix test
    refuses one row, `मालपोत कार्यालय`, and no other.

    Both titles are read (`title.ne` and `title.en`), deduped by IRI, and the
    winning candidate is excluded via `exclude` -- so neither a repeat search row
    nor the winner's own longer alternate title can manufacture a sibling.
    """
    small = name_tokens(extracted)
    if not small:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for result in candidates or ():
        nes_id = (result.get("id") or "").strip()
        if not nes_id or nes_id == exclude or nes_id in seen:
            continue
        title = result.get("title") or {}
        for form in (title.get("ne"), title.get("en")):
            longer = name_tokens(form or "")
            if len(longer) <= len(small):
                continue
            if all(tokens_equal(a, b) for a, b in zip(small, longer)):
                seen.add(nes_id)
                out.append(form)
                break
    return tuple(out)


def _unqualified_institution_veto(extracted: str, nes_id: str,
                                  candidates: list[dict]) -> str:
    """Reason the winning candidate is an unqualified institution bucket, or "".

    THE PROPERTY, which is structural rather than lexical: an institution-type
    name with no distinguishing qualifier cannot identify ONE specific office,
    because Nepal has one of these per district. Such a name must not bind, and
    NES's own candidate list is what proves the name is unqualified -- if NES
    holds `<this name> + <a locality>`, then `<this name>` alone is the family,
    not a member of it.

    Replaces a curated-vocabulary test. The old veto asked "is every token in
    GENERIC_TOKENS", which meant it fired only when the DOMAIN word was curated
    too: `जिल्ला वन कार्यालय` was held because Task 3 added वन, and the
    neighbouring `मालपोत कार्यालय` bound at 1.00 to
    `organization/malapota-karyalaya-44fbce`, a district-less bucket, because
    मालपोत was not on the list. NES holds 12 entities whose title contains
    मालपोत and 11 of them name their district. Extending the word list would
    have bought exactly one more round of the same game.

    Nothing fuzzy, nothing probabilistic: token equality via `tokens_equal`, the
    same comparison every other decision here uses, over a candidate list the
    caller already fetched. Cost on the labelled set: precision stays 1.000,
    recall 0.872 -> 0.846 (34 correct binds -> 33). The one row it costs is
    `मालपोत कार्यालय`, whose label is a human's bind on one specific case; the
    resolver would produce that same bind for a case in any other district,
    which is the behaviour being refused.

    Reads no district table and no locality list. A genuinely district-qualified
    name keeps binding, and it does not depend on recognising the district: the
    qualified name simply is not a prefix of anything.
    `भूमिसुधार कार्यालय नवलपरासी`, `जिल्ला प्राविधिक कार्यालय, मुगु` and
    `मालपोत कार्यालय, कलंकी` all have zero qualified siblings and all still bind.
    """
    if not names_an_institution(extracted):
        return ""
    siblings = qualified_siblings(extracted, candidates, exclude=nes_id)
    if not siblings:
        return ""
    shown = ", ".join(repr(title) for title in siblings[:3])
    return (
        f"unqualified institution name: NES holds {len(siblings)} entit"
        f"{'y' if len(siblings) == 1 else 'ies'} named '{extracted}' plus a "
        f"qualifier ({shown}), so this name is the family and {nes_id} is the "
        "member that names no locality. Nepal has one of these per district -- "
        "add the district to the name, then re-run."
    )


def resolve(extracted_name: str, candidates: list[dict]) -> Decision:
    """Decide what to do with one LLM-extracted name.

    BIND only when exactly ONE NES entity scores at or above MIN_BIND_SCORE and
    no veto applies. More than one qualifying entity is an ambiguity and goes to
    review -- 11 of the 142 labelled names hit that, with up to 13 same-name
    entities for "संजय प्रसाद यादव".

    A candidate whose @id is not a canonical entity IRI is dropped before
    scoring, so a malformed IRI can never reach the API.
    """
    best_by_id: dict[str, tuple[float, str, str]] = {}
    for result in candidates or ():
        nes_id = (result.get("id") or "").strip()
        if not is_valid_entity_iri(nes_id):
            continue
        best = max(
            ((match_score(extracted_name, form), form)
             for form in candidate_name_forms(result)),
            default=(0.0, ""),
        )
        if best[0] > 0 and best[0] > best_by_id.get(nes_id, (0.0, "", ""))[0]:
            # Two result dicts sharing one IRI (a repeat row from the search
            # API) must collapse to a single entry -- Task 7 writes
            # Decision.candidates into the review file a caseworker reads, and
            # the same entity must not be listed twice.
            best_by_id[nes_id] = (best[0], nes_id, best[1])
    scored = sorted(best_by_id.values(), key=lambda row: (-row[0], row[1]))
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
    # Last, because these two are the vetoes that depend on WHICH candidate won.
    province_veto = _province_veto(extracted_name, nes_id)
    if province_veto:
        return Decision(REVIEW, None, score, matched, province_veto, frozen)
    bucket_veto = _unqualified_institution_veto(extracted_name, nes_id, candidates or [])
    if bucket_veto:
        return Decision(REVIEW, None, score, matched, bucket_veto, frozen)
    return Decision(BIND, nes_id, score, matched, "", frozen)


# The `identifier` markers the Election Commission sourcing loads leave behind.
# A `PropertyValue` whose `propertyID` is in this set is the ONLY thing the veto
# keys on -- see `is_election_candidate_record` for why the slug and the
# occupation are not usable substitutes.
#
# Two loads, two markers, one hazard. Kept as a SET rather than a string because
# we already got this wrong once: the veto shipped keyed on `ecn-candidate-id`
# alone, and `nec-candidate-id` records walked straight through it. A third load
# adding a third marker is now a one-line change with an obvious home.
ELECTION_RECORD_MARKERS = frozenset({
    # docs/nes/sourcing/local-candidates/RESULTS.md -- 146,275 new Person
    # entities, every 2079 local-election candidate, winners AND losers.
    "ecn-candidate-id",
    # docs/nes/sourcing/ward-chairs/RESULTS.md -- ~6,743 ELECTED ward heads.
    # The worse half of the hazard: an elected official is likelier to turn up in
    # a CIAA case than a losing candidate, so a namesake collision here is both
    # more probable and more plausible-looking.
    "nec-candidate-id",
})


def _identifier_entries(document):
    """The `identifier` list of a document, whatever shape it arrived in.

    Defensive on purpose -- `identifier` may be absent, a single dict rather than
    a list, or hold members that are not dicts at all.
    """
    if not isinstance(document, dict):
        return ()
    entries = document.get("identifier")
    if entries is None:
        return ()
    if isinstance(entries, dict):
        entries = [entries]
    elif not isinstance(entries, (list, tuple)):
        return ()
    return tuple(e for e in entries if isinstance(e, dict))


def _election_marker(document) -> str:
    """Which election marker this document carries, or "" -- for the veto reason."""
    for entry in _identifier_entries(document):
        if entry.get("propertyID") in ELECTION_RECORD_MARKERS:
            return str(entry["propertyID"])
    return ""


def is_election_candidate_record(document) -> bool:
    """True when this NES entity document is an election candidate or ward-head record.

    WHY THIS EXISTS, because it looks like paranoia and a future reader will
    otherwise delete it. A large share of NES's 162,650 `person` entities come
    from two Election Commission sourcing loads:

        146,275  ecn-candidate-id  every 2079 local-election candidate, winners
                                   and losers, one row per contested post
                                   (docs/nes/sourcing/local-candidates/RESULTS.md)
        ~6,743   nec-candidate-id  the ELECTED head of each of Nepal's wards
                                   (docs/nes/sourcing/ward-chairs/RESULTS.md)

    Between them they cover essentially every ward in Nepal. Of 655 accused
    person binds that human caseworkers made on published Jawafdehi cases, only 2
    point at one. So an exact name match onto such a record is, absent
    corroboration, a DIFFERENT PERSON who happens to share the name.

    That is not hypothetical. Six of the 40 first-pass binds across the labelled
    set in `tests/casework/fixtures/` were namesake candidates in the wrong
    district. The clearest is `नन्दलाल दास`, bound to
    `person/nandlal-das-310567` — a Ward Member candidate for Katahariya
    Municipality ward 4 in RAUTAHAT, while case 080-CR-0064 names a former Germi
    VDC secretary in NAWALPARASI, and that case's own bind list (readable in
    prod) does not include him at all. The caseworker declined to bind him;
    the resolver did not. A second, positive proof: for `याङजी शेर्पा` a
    caseworker bound a DIFFERENT same-name entity on the same case, leaving the
    ECN record unbound.

    THE VETO IS NOT FREE, but it is cheaper than the raw rate suggests. Two
    measurements, on two different populations, and the gap between them is
    itself the argument:

        2 of 655 human accused binds on PUBLISHED cases target an ECN record   0.3%
        3 of  39 human accused binds on four DRAFT cases do                    7.7%

    PUBLISHED means it passed moderation; DRAFT means nobody has reviewed it. A
    25x gap between moderated and unmoderated data is not noise — if binding a
    candidate record were usually right it would survive moderation at a similar
    rate. Checking the three DRAFT ones against case locality says the same
    thing: two point at a ward in a district the case has nothing to do with,
    i.e. they are namesake errors a reviewer would have caught. Only
    `person/raj-bahadur-bam-318984` is locality-coherent (same gaunpalika as its
    case, though a different ward, with the role unstated).

    So the veto's cost in CORRECT binds is at most 1 in 39 (~2.6%), not 7.7% and
    not 10%, and its benefit includes declining to repeat two apparent human
    errors. It does the job moderation does, earlier and cheaper. What it is not
    is a claim that candidate records are never the subject — real officials
    stand for election, so a vetoed row is a REVIEW for a caseworker, never a
    NO_MATCH.

    Both markers count, and the second one was a real miss. This predicate
    originally keyed on `ecn-candidate-id` alone, which let the ~6,743
    `nec-candidate-id` ward-head records through. One of the 39 human DRAFT binds
    is such a record and it is a wrong-district bind:
    `person/tejnath-paudel-ward-51208-8` is Ward Chairperson of ward 8,
    Badhaiyatal Gaunpalika, in BARDIYA, bound as accused on a SOLUKHUMBU
    land-revenue case. With it counted, 3 of the 4 election-record binds a human
    made on those cases point at the wrong district.

    Keyed on the `identifier` markers alone, deliberately:

    * The SLUG is not a proxy. `person/nandlal-das-310567` ends in its ECN id,
      but `person/mohan-bahadur-basnet-334834` — a correct human bind, the
      accused on published case 081-CR-0060 — ends in a 6-digit internal id and
      carries no ECN marker. A slug-shape rule would veto real binds.
    * `hasOccupation` alone is softer: plenty of legitimately-bound officials
      have one, and the field is free-form.

    Defensive about shape on purpose — see `_identifier_entries`.
    """
    return any(entry.get("propertyID") in ELECTION_RECORD_MARKERS
               for entry in _identifier_entries(document))


def apply_document_veto(decision: Decision, document) -> Decision:
    """Downgrade a BIND to REVIEW when the bound entity is an ECN candidate record.

    Split out from `resolve` so `resolve` stays pure and the I/O stays with the
    caller — the entity document needs a second HTTP round trip per bind, which
    is exactly the kind of thing this module refuses to do itself:

        decision = resolve(name, api.search_entities(name))
        if decision.is_bind:
            decision = apply_document_veto(decision, api.get_entity(decision.nes_id))

    Anything that is not a BIND comes back untouched, so it is safe to call
    unconditionally. A vetoed decision keeps its `score`, `matched_name` and the
    full `candidates` tuple — Task 7 writes those into the file a caseworker
    reads, and dropping them would leave the reviewer with nothing to judge — but
    `nes_id` becomes None, because a REVIEW that still carries an id invites the
    very bind this veto just refused.

    FAILS CLOSED. An unreadable `document` — None, `{}`, a non-dict, anything but
    a non-empty dict — downgrades the BIND too, under a DIFFERENT reason. That
    matters because the caller hands us the result of a second HTTP read, and one
    transient failure on it (the WAF 403 the capture script's browser UA exists
    for, a 502, a 404 on an entity renamed between the two calls, an empty body)
    would otherwise bind राज बहादुर बम to `person/raj-bahadur-bam-318984`, an
    elected ward member in Kalikot — the exact wrong bind this veto was added to
    stop, restored by one HTTP hiccup. Unverified means REVIEW, never BIND.
    Returning REVIEW rather than raising keeps a bad read from aborting a whole
    enrichment run: it costs a caseworker one look, not the batch.

    The two reasons stay distinct so the review file can say which happened —
    "this is an election candidate" is a judgement, "I could not check" is a
    retry.
    """
    if not decision.is_bind:
        return decision
    if not isinstance(document, dict) or not document:
        return Decision(
            REVIEW,
            None,
            decision.score,
            decision.matched_name,
            "entity document unavailable, bind not verified: could not read "
            f"{decision.nes_id} to rule out an Election Commission candidate "
            "record. Retry the read, or confirm the entity by hand.",
            decision.candidates,
        )
    if not is_election_candidate_record(document):
        return decision
    # Name the marker that fired, so a caseworker can tell a losing candidate
    # (`ecn-candidate-id`) from a sitting ward head (`nec-candidate-id`).
    marker = _election_marker(document) or "an election-record identifier"
    return Decision(
        REVIEW,
        None,
        decision.score,
        decision.matched_name,
        "Election Commission record, not confirmed as the case subject: "
        f"{decision.nes_id} carries {marker}. Same-name election records "
        "outnumber real case subjects, so this needs a human look.",
        decision.candidates,
    )
