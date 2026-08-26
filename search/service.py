"""The unified search service: ONE multi-index query across the four indices,
merged into the common result envelope (unified-search plan §5).

Engine-agnostic + sqlite-testable: this module never touches a DB. It builds an
OpenSearch query DSL, runs it against ``make_client()``, and serializes the hits.
Tests patch ``make_client`` (or pass ``client=``) with a mock whose ``.search``
returns a canned OpenSearch response — no live cluster required.

Hard dependency (decision #5): if OpenSearch is unreachable the service raises
``SearchUnavailable`` (the view maps it to 503). There is NO in-process fallback.

ACL: the index is all-public (drafts/in-review cases are never indexed), so there
is NO visibility/ACL filter — search is fully public-read.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any

from jawafdehi_shared.search.aliases import generation_ordinal
from jawafdehi_shared.search.opensearch import (
    CASE_INDEX,
    COURTCASE_INDEX,
    ENTITY_INDEX,
    MATERIAL_INDEX,
    make_client,
)

from .analytics import normalize_query

logger = logging.getLogger("jawafdehi.search")

# Result ``type`` token  ->  the index that holds it + the owning source_app.
TYPE_TO_INDEX: dict[str, str] = {
    "entity": ENTITY_INDEX,
    "material": MATERIAL_INDEX,
    "courtcase": COURTCASE_INDEX,
    "case": CASE_INDEX,
}
INDEX_TO_TYPE: dict[str, str] = {v: k for k, v in TYPE_TO_INDEX.items()}
ALL_TYPES: tuple[str, ...] = ("entity", "material", "courtcase", "case")


def type_for_index(index: str) -> str | None:
    """Result type for the index a hit came from, or None if it isn't ours.

    We QUERY the four public names, but every name is now an ALIAS over a
    numbered generation, and OpenSearch reports the CONCRETE backing index on
    both a hit's ``_index`` and an ``_index`` aggregation bucket — never the
    alias we asked for. So a straight ``INDEX_TO_TYPE`` lookup started missing
    the moment the aliases landed: ``jawafdehi-cases-000001`` is not a key.

    Strip a trailing generation suffix and retry, so both the aliased and the
    pre-alias plain-index shapes resolve. Anything else is not one of ours.
    """
    result_type = INDEX_TO_TYPE.get(index)
    if result_type is not None:
        return result_type
    for alias, candidate in INDEX_TO_TYPE.items():
        if generation_ordinal(alias, index) is not None:
            return candidate
    return None

# ── Relevance weights (the ONE place to tune ranking) ───────────────────────────
#
# Field boosts for the bilingual multi_match (title > keywords > body).
# ``most_fields`` SUMS the per-field scores so a hit across several language
# subfields ranks above a single-field hit (the research-mandated names strategy).
# Native-script + roman titles outrank the ``.translit`` recall bridge so a true
# same-script match beats a transliterated near-match.
_TITLE_NE_BOOST = 3.0
_TITLE_EN_BOOST = 3.0
_TITLE_TRANSLIT_BOOST = 2.0  # cross-script recall bridge — below native-script
_KEYWORDS_BOOST = 2.0
_BODY_BOOST = 1.0

# Base field set (lang="both"). ``build_query`` re-weights the title fields when a
# specific ``lang`` is requested (see ``_weighted_query_fields``).
QUERY_FIELDS: list[str] = [
    f"title_ne^{_TITLE_NE_BOOST:g}",
    f"title_en^{_TITLE_EN_BOOST:g}",
    f"title_translit^{_TITLE_TRANSLIT_BOOST:g}",
    f"keywords.text^{_KEYWORDS_BOOST:g}",
    f"body^{_BODY_BOOST:g}",
]

# The title fields a query-phrase clause matches against for the EXACT-PHRASE
# boost: when the query terms appear adjacently in a title (e.g. the full name
# "Sher Bahadur Deuba"), add a strong bonus so exact/near-exact title hits float
# above documents that merely contain the terms scattered in the body.
PHRASE_FIELDS: list[str] = ["title_ne", "title_en", "title_translit"]
PHRASE_BOOST = 5.0

# ── Bounded fuzzy matching (design §10) ─────────────────────────────────────────
#
# Romanized Nepali has no fixed spelling, so a near-miss query (``coruption``,
# ``baluwatar``) matched nothing at all and dead-ended on the empty state. This is
# a DAMPED LAST-RESORT recall route for exactly that, never a general matching
# strategy: it rides as a second ``should`` beside the exact recall clause, and
# ``FUZZY_BOOST`` keeps whatever it drags in below every correctly-spelled match.
#
# Fields mirror the exact route's relative weighting (title > keywords > body) by
# reusing the SAME boost constants, so re-tuning one route cannot silently desync
# the other. Two deliberate differences from ``_weighted_query_fields``:
#
#   * ``title_ne`` is EXCLUDED. Fuzziness is Levenshtein over analyzed terms, and
#     a Roman token is never within two edits of a Devanagari one — the clause
#     would cost expansions and match nothing. (Devanagari fuzziness is out of
#     scope per design §10; it keeps normalization + the translit bridge.)
#   * no ``lang`` re-weighting. Re-ranking WITHIN a route that is already damped
#     below every exact match buys nothing.
FUZZY_FIELDS: list[str] = [
    f"title_en^{_TITLE_EN_BOOST:g}",
    f"title_translit^{_TITLE_TRANSLIT_BOOST:g}",
    f"keywords.text^{_KEYWORDS_BOOST:g}",
    f"body^{_BODY_BOOST:g}",
]

# Below ``_BODY_BOOST`` (the weakest exact field) and far below ``PHRASE_BOOST``,
# so design §10's "a fuzzy match must never outrank an exact one" holds by
# construction rather than by hope. BM25 cannot make that a HARD guarantee — this
# is the knob, and ``test_fuzzy_boost_stays_below_every_exact_weight`` is the
# watchdog.
FUZZY_BOOST = 0.3

# ``AUTO:4,8`` — under 4 chars exact, 4–7 one edit, 8+ two edits. Two is the
# ceiling design §10 allows; raising it makes results junky (at 3 edits
# ``deuba``/``duba``-class collisions arrive faster than real corrections).
FUZZINESS = "AUTO:4,8"

# The first character must match. Cheaper (it prunes the term-dictionary walk) and
# markedly less noisy — most genuine romanization slips are interior.
FUZZY_PREFIX_LENGTH = 1

# Eligibility (design §10): Roman script, at least four characters, no
# identifiers/case numbers/numerics, nothing denylisted. The denylist ships EMPTY
# on purpose — it is meant to be populated from the zero-result analytics stream
# (``search/analytics.py``), measured rather than guessed.
FUZZY_MIN_TOKEN_LENGTH = 4
FUZZY_DENYLIST: frozenset[str] = frozenset()

# ── Did-you-mean suggestions (design §11) ───────────────────────────────────────
#
# A ``term`` suggester riding on the SAME request as the search — no second round
# trip — over the two fields whose vocabulary is worth suggesting from:
#
#   * ``keywords.text``  — curated tags, i.e. design §11's "approved aliases"
#     (and improving as the ``case_tags`` vocabulary lands).
#   * ``title_translit`` — unstemmed ASCII romanizations of every indexed title.
#
# NOT ``title_en``: it is Porter-stemmed, so its term dictionary holds ``corrupt``
# and it would suggest that for ``coruption``. NOT ``body``: OCR garbage is exactly
# the vocabulary a suggestion must not come from.
#
# ORDER IS AUTHORITY, most-trusted first — ``_suggested_replacements`` breaks ties
# by position here before it looks at score. Measured against the live corpus:
# ``melamchee`` draws ``melamchi`` (score 0.75) from the curated tags and
# ``maramchee`` (0.78) from the romanizations, so ranking on score alone surfaces
# the junk. ``title_translit`` holds ONE machine transliteration per title, which
# makes its near-neighbours mostly noise (``maramchee``, ``melamchhi``,
# ``melamchil`` — all ``freq: 1``); a human-curated tag is the better answer
# whenever the two disagree.
SUGGEST_FIELDS: tuple[str, ...] = ("keywords.text", "title_translit")

# ``missing`` mode only suggests for terms absent from the index, which makes the
# suggester near-free for a well-spelled query. ``size: 1`` because design §11
# asks for at most one primary suggestion; the rest mirror the fuzzy bounds above.
SUGGEST_MODE = "missing"
SUGGEST_MAX_EDITS = 2
SUGGEST_PREFIX_LENGTH = 1
SUGGEST_MIN_WORD_LENGTH = 4
SUGGEST_SIZE = 1

# When ``lang`` narrows to one script, multiply that script's title boost so
# same-language matches rank first WITHOUT excluding the other (cross-script
# recall via the translit bridge is preserved — this is a re-rank, not a filter).
_LANG_TITLE_MULTIPLIER = 2.0

# Per-type (per-index) weighting: published editorial records (cases) are the
# flagship, human-curated answers and are boosted hardest so a matching case
# floats above the far larger pool of entities/court-case stubs/archive materials
# at comparable textual score; curated entities come next. Applied via OpenSearch
# ``indices_boost``. Text relevance still dominates within an index — this
# re-ranks ACROSS indices, and (unlike a filter) a case must still MATCH the query
# to benefit, so the colloquial ``title_translit`` recall fix is what lets a
# Devanagari-titled case match a Latin query in the first place.
TYPE_BOOSTS: dict[str, float] = {
    "case": 2.0,
    "entity": 1.2,
    "courtcase": 1.0,
    "material": 0.9,
}

MAX_PAGE_SIZE = 50

# Offset (``from``/``size``) paging is cheap for shallow pages but OpenSearch
# rejects ``from + size`` beyond ``index.max_result_window`` (default 10,000).
# Past that, callers must page with an opaque ``cursor`` (``search_after``), which
# has no depth limit. This is the offset ceiling we allow before requiring a
# cursor (kept under the 10k window with headroom for the largest page).
MAX_OFFSET_RESULT_WINDOW = 10_000

# Deterministic, total sort order for cursor (search_after) paging: primary by
# relevance (desc), tie-broken by the unique ``iri`` keyword (asc) so the order is
# stable and every document is reachable. ``_score`` MUST be paired with a unique
# tiebreaker or search_after can skip/repeat rows at score ties.
SORT_SPEC: list[dict[str, Any]] = [
    {"_score": {"order": "desc"}},
    {"iri": {"order": "asc"}},
]

# Allowed ``sort`` modes. Every spec ends with the unique ``iri`` tiebreaker so
# search_after cursor paging stays stable + complete regardless of the primary key
# (a non-unique primary like ``date`` can skip/repeat rows without it).
SORT_RELEVANCE = "relevance"
ALL_SORTS: tuple[str, ...] = ("relevance", "newest", "oldest", "title", "featured")


def _sort_spec(sort: str) -> list[dict[str, Any]]:
    """The OpenSearch ``sort`` clause for a ``sort`` mode (defaults to relevance).

    ``newest``/``oldest`` order by the Gregorian ``date`` field (missing dates sort
    last either way); ``title`` orders by the untokenized ``title_en.keyword``
    subfield. ``featured`` orders by the editorial ``weight`` (cases only), then
    falls back to ``newest``. Every mode appends the ``iri`` tiebreaker for stable
    cursor paging.
    """
    if sort == "featured":
        # ``missing: 0`` so a doc indexed before ``weight`` existed ranks as unranked
        # rather than below an explicit 0. ``unmapped_type`` because sorting on a
        # field absent from the index MAPPING is a hard error, not a graceful skip —
        # create_index no-ops on an existing index, so no live index carries
        # ``weight`` until the next reindex. With it the sort degrades to ``newest``.
        return [
            {"weight": {"order": "desc", "missing": 0, "unmapped_type": "integer"}},
            {"date": {"order": "desc", "missing": "_last"}},
            {"iri": {"order": "asc"}},
        ]
    if sort == "newest":
        return [{"date": {"order": "desc", "missing": "_last"}}, {"iri": {"order": "asc"}}]
    if sort == "oldest":
        return [{"date": {"order": "asc", "missing": "_last"}}, {"iri": {"order": "asc"}}]
    if sort == "title":
        return [
            {"title_en.keyword": {"order": "asc", "missing": "_last"}},
            {"iri": {"order": "asc"}},
        ]
    return SORT_SPEC


# Facet/filter fields: the request param name -> the keyword index field it filters
# and aggregates over. ``entity_type`` reuses the schema.org ``type`` token; ``tags``
# reuses the shared ``keywords`` field; the ``status`` param filters the coarse
# case lifecycle, backed by the dedicated ``case_status`` field (NOT the generic
# ``status``, which holds NGM's scraper enrichment flag). These are exact-match
# (``terms``) facets, distinct from the per-type ``counts`` (from the ``_index`` agg).
FACET_FIELDS: dict[str, str] = {
    "entity_type": "type",
    "case_type": "case_type",
    "tags": "keywords",
    "status": "case_status",
    "court": "court",
    "court_type": "court_type",
    "district": "court_district",
    "province": "court_province",
}

# The closed vocabulary behind the ``court_type`` facet: Nepal's constitutional
# court tiers, and the only four values ``Court.court_type`` holds (verified
# against production: 77 district / 18 high / 1 supreme / 1 special).
#
# ONE definition because the tier list has three consumers that must agree — the
# serializer's ``ChoiceField`` (what actually 400s), the OpenAPI ``enum`` (what
# the SPA reads), and the MCP tool's static schema (what a model reads). The MCP
# copy stays a literal so that schema builds without Django, exactly like
# ``sort``, and is pinned to this tuple by
# ``test_search_court_type_enum_tracks_all_court_types``.
ALL_COURT_TYPES: tuple[str, ...] = ("district", "high", "supreme", "special")

# Bucket count for each facet's ``terms`` aggregation. Most vocabularies fit
# comfortably under the default; an entry here overrides it for the ones that
# don't (e.g. a district facet must hold all 77 districts at once — at the
# default, real buckets would be silently pushed out and their counts zeroed).
DEFAULT_FACET_AGG_SIZE = 50
FACET_AGG_SIZES: dict[str, int] = {
    # All 97 courts (77 district + 18 high + supreme + special) + headroom. Every
    # one of them carries cases, so at the default size a third of the courts
    # would be missing from the facet with their counts silently zeroed — and this
    # is the facet a court picker is built from, so the gap would be user-visible.
    "court": 150,
    # 77 districts + headroom (no sentinel: only district courts carry one).
    "district": 100,
    # 7 provinces + NATIONAL.
    "province": 10,
}


# Lucene RegExp operator characters (the core set plus every optional-operator
# character, which OpenSearch may enable via flags) — escaped in facet_q text.
_LUCENE_REGEXP_SPECIAL = frozenset('.?+*|{}[]()"\\#@&<>~')

# Longest ``facet_q`` text accepted, in CODE POINTS (``len()``, so a Devanagari
# combining mark counts on its own).
#
# A HARD bound, not a nicety: the text is expanded into a Lucene RegExp and
# determinized into an automaton ON THE CLUSTER, and every cased letter widens to
# a ``[xX]`` class, so the emitted pattern is up to 4n+4 characters. Past a point
# the determinization throws and ``search()``'s blanket ``except Exception`` can
# only report that as ``SearchUnavailable`` — a 503 plus a Sentry search-outage
# event for what is plainly a bad request. Same reasoning as
# ``bigo_min``/``bigo_max``'s ``max_value``: reject it at the edge rather than
# mislabel the failure.
#
# Where the point actually is. Lucene 9's ``Operations.determinize`` spends
# ``effort += |subset|`` per popped powerset state against
# ``effortLimit = determinizeWorkLimit * 10``, i.e. 100,000 for the default
# 10,000 — the ``* 10`` is easy to miss and puts the ceiling an order of
# magnitude above the bare limit. For the ``.*<text>.*`` shape emitted here the
# worst case is a REPEATED cased letter (overlapping subsets, KMP-like), costing
# n*(n+2); distinct letters are ~10x cheaper and uncased scripts cheaper still.
# So:
#     n=200 -> 40,400 (40% of budget), pattern 804 chars
#     n=315 -> 99,855 (the last value that passes)
#     n=316 -> throws
#
# Why 200 and not a rounder number: it is the longest ``case_type`` value the
# production corpus actually holds (2,332 distinct values in the NGM docket, 240
# of them over 64 code points), and ``case_type`` is a facet_q-able facet. A
# lower cap still works as a typeahead — the include is a CONTAINS match, so a
# prefix selects the same bucket — but it 400s a client that reads a key out of
# ``facets.case_type`` and pastes it straight back, which is exactly what the MCP
# tool now tells a model those values are for. 200 keeps every real key
# expressible at 40% of the determinize budget, and leaves the pattern under the
# 1,000-character ``index.max_regex_length`` default should that ever apply to a
# terms-agg ``include`` (it governs ``regexp`` queries; unverified here).
MAX_FACET_Q_TEXT = 200


def _facet_include_regex(text: str) -> str:
    """A Lucene-RegExp ``include`` pattern matching bucket keys that CONTAIN
    ``text``, case-insensitively — for the ``facet_q`` facet-value search.

    Lucene RegExp (what a ``terms`` agg's ``include`` speaks) has no ``(?i)``
    flag, so case-insensitivity is spelled out as a ``[xX]`` class per cased
    letter. Only the Lucene operator characters are backslash-escaped — so user
    text can never smuggle ``.*``/``|``/``{}`` into the aggregation — and every
    other character (Devanagari letters AND combining vowel signs included)
    passes through verbatim.
    """
    parts: list[str] = []
    for ch in text:
        lower, upper = ch.lower(), ch.upper()
        if lower != upper and len(lower) == 1 and len(upper) == 1:
            parts.append(f"[{lower}{upper}]")
        elif ch in _LUCENE_REGEXP_SPECIAL:
            parts.append("\\" + ch)
        else:
            parts.append(ch)
    return ".*" + "".join(parts) + ".*"

# RANGE filters: the request param name -> (indexed field, the ``range`` bound it
# sets). The second filter KIND, alongside the exact-match ``terms`` facets above
# — every filter before this one was exact-match, and there was no range path in
# the query builder at all.
#
# Params sharing a field are merged into ONE ``range`` clause, so
# ``?bigo_min=X&bigo_max=Y`` becomes a single bounded interval rather than two
# clauses that read as unrelated constraints.
#
# ``bigo`` is CASE-ONLY: no entity/material/court-case document carries an amount,
# so a bound excludes every non-case hit. That is the same shape as the ``status``
# facet above (also case-only, also applied globally) and callers should pair a
# bound with ``?type=case``; the API view's OpenAPI description says so outright.
#
# ``date_from``/``date_to`` bound the shared Gregorian ``date`` field — exactly
# the two entries the field-agnostic mechanism was built for, PLUS the two
# matching ``DateField``s on ``SearchQuerySerializer``. Both halves, always: the
# view reads bounds out of ``validated_data``, and DRF discards any param the
# serializer does not declare, so an entry added here alone is accepted and then
# silently ignored — no clause, no 400, no log.
# ``test_every_range_field_is_declared_on_the_query_serializer`` fails if the two
# ever drift. The clause-building itself is genuinely field-agnostic, which is
# the point: a second range mechanism never gets built.
#
# ``date`` scoping: entities never index a ``date`` (and a court case with no
# ``registration_date_ad`` carries none either), so a date bound excludes those
# docs — the same shape as ``bigo``, documented on the OpenAPI params.
RANGE_FIELDS: dict[str, tuple[str, str]] = {
    "bigo_min": ("bigo", "gte"),
    "bigo_max": ("bigo", "lte"),
    "date_from": ("date", "gte"),
    "date_to": ("date", "lte"),
}


def _range_clauses(ranges: dict[str, Any] | None) -> list[dict[str, Any]]:
    """``range`` filter clauses for the given bounds (one clause per field).

    Iterates :data:`RANGE_FIELDS` (not the caller's dict) so unknown params are
    ignored and the emitted DSL is byte-stable regardless of query-string order.

    A bound of ``None`` means "not requested" and is skipped. The test is
    ``is None``, NOT falsiness: ``0`` is a legitimate lower bound and must survive.
    """
    bounds: dict[str, dict[str, Any]] = {}
    for param, (field, bound) in RANGE_FIELDS.items():
        value = (ranges or {}).get(param)
        if value is None:
            continue
        bounds.setdefault(field, {})[bound] = value
    return [{"range": {field: b}} for field, b in bounds.items()]


class SearchError(Exception):
    """A client-side search error (→ HTTP 400), e.g. a malformed cursor or an
    offset page past the result window. Distinct from :class:`SearchUnavailable`
    (a 503 infrastructure failure)."""


def encode_cursor(sort_values: list[Any]) -> str:
    """Encode an OpenSearch hit's ``sort`` values into an opaque page cursor.

    The cursor is the ``sort`` array of the LAST hit on a page; the next page is
    fetched with ``search_after=<those values>``. Base64url(JSON) so it is a safe,
    opaque query-string token (clients must treat it as a black box)."""
    raw = json.dumps(sort_values, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> list[Any]:
    """Decode a page cursor back into ``search_after`` sort values.

    Raises :class:`SearchError` (→ 400) on any malformed/garbage token rather than
    letting a decode error surface as a 500."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        values = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise SearchError(f"invalid cursor: {exc}") from exc
    if not isinstance(values, list):
        raise SearchError("invalid cursor: expected a list of sort values")
    return values


class SearchUnavailable(Exception):
    """Raised when the OpenSearch cluster is unreachable (→ HTTP 503).

    Hard dependency: there is no fallback search. The view catches this and
    returns 503 "search temporarily unavailable".
    """


def _index_for_types(types: list[str] | None) -> str:
    """Comma-joined index list for the requested ``type`` filter (all if None)."""
    selected = types or list(ALL_TYPES)
    return ",".join(TYPE_TO_INDEX[t] for t in selected if t in TYPE_TO_INDEX)


def _weighted_query_fields(lang: str) -> list[str]:
    """Field list for the recall ``multi_match``, re-weighted for ``lang``.

    ``lang`` is a soft re-rank, never a filter: for ``ne``/``en`` the matching
    native-script title boost is multiplied so same-language hits rank first while
    the other script (and the translit bridge) still contribute recall.
    """
    if lang == "ne":
        ne, en = _TITLE_NE_BOOST * _LANG_TITLE_MULTIPLIER, _TITLE_EN_BOOST
    elif lang == "en":
        ne, en = _TITLE_NE_BOOST, _TITLE_EN_BOOST * _LANG_TITLE_MULTIPLIER
    else:  # "both" (default)
        ne, en = _TITLE_NE_BOOST, _TITLE_EN_BOOST
    return [
        f"title_ne^{ne:g}",
        f"title_en^{en:g}",
        f"title_translit^{_TITLE_TRANSLIT_BOOST:g}",
        f"keywords.text^{_KEYWORDS_BOOST:g}",
        f"body^{_BODY_BOOST:g}",
    ]


def fuzzy_eligible_tokens(q: str | None) -> list[str]:
    """The query tokens bounded fuzzy matching may be applied to (design §10).

    Splits the SAME :func:`normalize_query` the analytics stream aggregates on (NFC
    + trim + lowercase + whitespace-collapse), then keeps a token only when every
    character is an ASCII letter, it is at least :data:`FUZZY_MIN_TOKEN_LENGTH`
    long, and it is not denylisted.

    That one ASCII-letters test delivers four of design §10's five exclusions at
    once, which is why there is no separate identifier/numeric detector here:
    Devanagari fails ``isascii``, and a case number (``082-CR-0154``), a bare year
    (``2024``) and any other identifier all fail ``isalpha`` on their digits and
    separators. A mixed query keeps only its eligible tokens — the ineligible ones
    are still matched exactly by the recall clause, they just never get fuzzed.

    Returns ``[]`` for a browse, a pure-Devanagari query or a bare identifier,
    which is the signal ``build_query`` uses to emit today's DSL untouched.
    """
    tokens: list[str] = []
    for token in normalize_query(q).split():
        if len(token) < FUZZY_MIN_TOKEN_LENGTH:
            continue
        if not (token.isascii() and token.isalpha()):
            continue
        if token in FUZZY_DENYLIST:
            continue
        tokens.append(token)
    return tokens


def _fuzzy_clause(tokens: list[str]) -> dict[str, Any]:
    """The damped fuzzy recall ``multi_match`` for the eligible tokens.

    Only the ELIGIBLE tokens are queried, not the raw ``q``: passing the whole
    string back would re-admit the Devanagari/identifier terms that eligibility
    just excluded, and ``fuzziness`` applies per term.

    ``most_fields`` (not ``cross_fields``) because cross_fields silently DROPS
    fuzziness — see ``docs/shared/research/opensearch-bilingual-nepali.md`` §5.
    """
    return {
        "multi_match": {
            "query": " ".join(tokens),
            "fields": FUZZY_FIELDS,
            "type": "most_fields",
            "operator": "or",
            "fuzziness": FUZZINESS,
            "prefix_length": FUZZY_PREFIX_LENGTH,
            "boost": FUZZY_BOOST,
        }
    }


def _suggest_block(tokens: list[str]) -> dict[str, Any]:
    """The ``suggest`` request block feeding ``did_you_mean`` (design §11).

    One ``term`` entry per :data:`SUGGEST_FIELDS`, both over the same ``text`` (the
    eligible tokens). Entries are keyed by their field name so the response parser
    needs no separate name→field map.

    No ``collate`` in v1: the suggestion vocabulary comes from fields the query
    already searches, so a suggested query is guaranteed at least one hit and there
    is nothing to verify a candidate against.
    """
    block: dict[str, Any] = {"text": " ".join(tokens)}
    for field in SUGGEST_FIELDS:
        block[field] = {
            "term": {
                "field": field,
                "suggest_mode": SUGGEST_MODE,
                "max_edits": SUGGEST_MAX_EDITS,
                "prefix_length": SUGGEST_PREFIX_LENGTH,
                "min_word_length": SUGGEST_MIN_WORD_LENGTH,
                "size": SUGGEST_SIZE,
            }
        }
    return block


def _indices_boost() -> list[dict[str, float]]:
    """Per-index weight list (``indices_boost``) from :data:`TYPE_BOOSTS`.

    Skips weights of exactly 1.0 (no-ops) so the emitted DSL stays minimal.
    """
    boosts: list[dict[str, float]] = []
    for result_type, weight in TYPE_BOOSTS.items():
        if weight != 1.0:
            boosts.append({TYPE_TO_INDEX[result_type]: weight})
    return boosts


def build_query(
    *,
    q: str,
    types: list[str] | None = None,
    lang: str = "both",
    sort: str = SORT_RELEVANCE,
    filters: dict[str, list[str]] | None = None,
    ranges: dict[str, Any] | None = None,
    facet_queries: dict[str, str] | None = None,
    page: int = 1,
    page_size: int = 10,
    search_after: list[Any] | None = None,
) -> dict[str, Any]:
    """Build the OpenSearch request body for query ``q`` (bilingual, tuned).

    Pure/inspectable so tests can assert the DSL (e.g. that ``title_translit`` is
    queried, that an exact-phrase title clause is present). ``type`` filtering is
    done by index selection (see ``_index_for_types``), not a query clause, so this
    body is index-agnostic. Ranking has three tuned layers (see the weight
    constants above):

    1. a ``most_fields`` multi_match across the bilingual title/keywords/body
       fields (recall + summed per-field score), re-weighted by ``lang``;
    2. a SHOULD ``multi_match`` ``phrase`` clause over the title fields that adds
       :data:`PHRASE_BOOST` when the query terms appear adjacently in a title, so
       exact/near-exact name matches float above scattered-term body matches;
    3. ``indices_boost`` (:data:`TYPE_BOOSTS`) nudging primary editorial records
       (cases/entities) above raw materials at near-equal textual score;
    4. for a query carrying at least one fuzzy-ELIGIBLE token (design §10 — see
       :func:`fuzzy_eligible_tokens`), a second, :data:`FUZZY_BOOST`-damped recall
       route beside (1) inside a satisfied-by-either nested bool, so a misspelled
       romanization still matches. A ``suggest`` block rides along on the same
       request to populate ``did_you_mean`` (design §11). Both are omitted
       entirely when no token is eligible.

    Paging: a deterministic :data:`SORT_SPEC` (score desc, ``iri`` asc tiebreaker)
    is ALWAYS applied so results are stable and cursorable. When ``search_after``
    is given (the previous page's last-hit sort values), ``from`` is omitted and
    OpenSearch resumes after that point — unbounded deep paging with no
    ``max_result_window`` ceiling. Otherwise shallow offset (``from``/``size``)
    paging is used.

    Narrowing comes in two kinds, both ANDed into the bool ``filter`` (no scoring
    impact): exact-match ``terms`` from ``filters`` (:data:`FACET_FIELDS`) and
    numeric/date ``range`` bounds from ``ranges`` (:data:`RANGE_FIELDS`).

    Per-type facet counts come from a ``_index`` terms aggregation (one index per
    type — exact regardless of ``source_app``, which is not 1:1 with type since
    ngm owns both materials and courtcases).

    ``facet_queries`` ({facet param: text}) is a facet-VALUE search: it adds a
    case-insensitive ``include`` regex to the named facet's terms agg so only
    buckets whose key contains the text come back — the query, hits, count and
    every other facet are untouched.
    """
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))

    # Exact-match facet filters (entity_type/case_type/tags) compose with the text
    # query as bool ``filter`` clauses (no scoring impact, just narrowing).
    #
    # The two clause KINDS are built apart because they merge differently: bounds
    # sharing a field collapse into ONE ``range`` clause, so ?bigo_min=X&bigo_max=Y
    # is a single bounded interval rather than two unrelated constraints.
    terms_clauses: list[dict[str, Any]] = []
    for param, values in (filters or {}).items():
        field = FACET_FIELDS.get(param)
        if field and values:
            terms_clauses.append({"terms": {field: list(values)}})
    # Range filters (bigo_min/bigo_max) narrow the same way — ANDed alongside the
    # exact-match ones, and equally inert for scoring.
    range_clauses = _range_clauses(ranges)
    filter_clauses: list[dict[str, Any]] = [*terms_clauses, *range_clauses]

    # ``q`` is OPTIONAL. With a term, build the tuned recall+precision bool query;
    # with an empty/blank ``q`` it's a BROWSE — ``match_all`` so the facet filters,
    # type selection, sort and paging still apply (list/page the corpus with no
    # search term). An empty multi_match would match nothing, so we must branch.
    has_query = bool(q and q.strip())
    # Which of the query's tokens bounded fuzziness may touch (design §10).
    # Computed ONCE — the fuzzy recall clause and the did-you-mean suggester below
    # are gated on the same list, so they can never disagree about eligibility.
    fuzzy_tokens = fuzzy_eligible_tokens(q) if has_query else []
    exact_recall_clause: dict[str, Any] = {
        "multi_match": {
            "query": q,
            "fields": _weighted_query_fields(lang),
            "type": "most_fields",
            "operator": "or",
        }
    }
    # Resolved here rather than inside ``bool_query`` below so all three modes —
    # browse, plain query, fuzzy-eligible query — share ONE bool shape.
    must_clauses: list[dict[str, Any]]
    if not has_query:
        must_clauses = [{"match_all": {}}]
    elif fuzzy_tokens:
        # A nested bool INSIDE ``must``, not a top-level ``should``: a pure
        # misspelling matches neither the exact recall clause nor the phrase
        # clause, and a top-level should cannot rescue an unsatisfied must — the
        # query would still return nothing, which is the whole bug. Wrapping the
        # two routes in one satisfied-by-either clause is what makes ``coruption``
        # reach ``corruption`` at all.
        must_clauses = [
            {
                "bool": {
                    "should": [exact_recall_clause, _fuzzy_clause(fuzzy_tokens)],
                    "minimum_should_match": 1,
                }
            }
        ]
    else:
        # No eligible token — Devanagari, a case number, a browse. The emitted DSL
        # is byte-identical to the pre-fuzzy one, deliberately: the mechanism must
        # be invisible on every query it cannot help.
        must_clauses = [exact_recall_clause]
    # ONE query shape for both modes. Hoisting ``must_clauses`` above already
    # absorbed the difference that used to justify a branch — ``match_all`` in
    # browse mode, ``multi_match`` with a term — so ``must`` and ``filter`` are now
    # the same expression either way. Keeping two arms meant a change to "the real
    # query" arm silently skipped browse mode, which is the primary way this
    # filter is used (a बिगो range with no search term).
    bool_query: dict[str, Any] = {
        # Recall clause: at least one of the bilingual fields must match, or
        # match_all when browsing.
        "must": must_clauses,
        # Exact-match facet + range narrowing (empty when nothing is requested).
        "filter": filter_clauses,
    }
    if has_query:
        # The only thing a search term adds: an adjacent-term (phrase) title match
        # that boosts score without being required, so single-term queries still
        # match. Meaningless while browsing, where every document scores alike.
        bool_query["should"] = [
            {
                "multi_match": {
                    "query": q,
                    "fields": PHRASE_FIELDS,
                    "type": "phrase",
                    "boost": PHRASE_BOOST,
                }
            }
        ]

    # Aggregations: per-type ``counts`` (by physical index) PLUS the exposed
    # facets (entity_type via the schema.org ``type`` token, case_type, and tags
    # via ``keywords``).
    #
    # Facet counts reflect the active FILTERS as well as the query: the filters
    # are ``bool.filter`` clauses on the main query (not a ``post_filter``), so
    # every agg is computed over the narrowed result set. Two consequences worth
    # knowing before changing this:
    #   - CASCADING, which callers rely on: filtering ``court_type=high`` empties
    #     the ``district`` facet outright, because no high-court doc carries a
    #     ``court_district`` — that empty bucket list is how a client knows the
    #     district refine does not apply to the current selection.
    #   - COLLAPSING, the cost of the same behaviour: a facet also narrows by its
    #     OWN filter, so selecting one court leaves ``facets.court`` with a single
    #     bucket and no sibling counts to widen the selection with. Clients drive
    #     a court picker off GET /api/courts/ (all 97, with names) and read this
    #     facet for counts only. Fixing that properly means a per-facet ``filter``
    #     agg applying every filter EXCEPT its own; a ``post_filter`` is NOT a
    #     substitute, as it would make every facet ignore every filter and so
    #     destroy the cascading above.
    #
    # Built as its own ``dict[str, Any]`` rather than inline in ``body``: the
    # nested literal would otherwise pin a narrow value type that the extent agg
    # below — which nests an ``aggs`` of its own — does not fit.
    aggs: dict[str, Any] = {
        # Sized 2x the type count, not 1x: the bucket key is the CONCRETE
        # backing index, and mid-swap an alias can briefly resolve to two
        # generations. At exactly len(ALL_TYPES) the extra bucket would push
        # a real one out and silently zero that type's facet count.
        "by_index": {"terms": {"field": "_index", "size": 2 * len(ALL_TYPES)}},
    }
    # One ``terms`` agg per exposed refine facet, GENERATED from FACET_FIELDS so a
    # facet param can never exist without its aggregation. These used to be
    # hand-listed alongside ``by_index``, which left a trap: a FACET_FIELDS entry
    # with no matching agg here validated fine, filtered fine, and then served an
    # empty ``facets.<param>`` list forever — no error, no log. Driving the aggs
    # off the registry closes that by construction (``by_index`` and the extent
    # agg below stay hand-written: they are not FACET_FIELDS facets).
    for param, field in FACET_FIELDS.items():
        aggs[param] = {
            "terms": {"field": field, "size": FACET_AGG_SIZES.get(param, DEFAULT_FACET_AGG_SIZE)}
        }
        # ``facet_q``: recompute ONLY this facet's bucket list to the top buckets
        # whose key contains the text. ``include`` filters the term set BEFORE
        # the size cut, so the match runs over the full aggregation, not the
        # default top-N slice — and it touches nothing but this one agg: the
        # query, count, hits and every other facet are computed exactly as
        # without it. Ordering stays the terms-agg default (count desc).
        text = (facet_queries or {}).get(param)
        if text:
            aggs[param]["terms"]["include"] = _facet_include_regex(text)

    # बिगो extent: the smallest and largest recorded amount, how many documents
    # carry one at all — the three numbers the SPA's slider ladder is cut from.
    #
    # ``global`` on purpose — the ONE aggregation here that must NOT reflect the
    # query or the filters. Every other agg above is a refine facet, where
    # narrowing along with the result set is the wanted behaviour. The extent is
    # not: if it tracked the active range, dragging a thumb inward would pull the
    # track in behind it and the reader could never widen back out. ``global``
    # escapes the query context entirely, so the scale stays a fixed property of
    # the corpus no matter what is typed or filtered.
    #
    # Requested ONLY for a case-only search — not merely when the case index is
    # somewhere in scope.
    #
    # A ``global`` agg is not a cheap re-label of the result set: it escapes the
    # query context by running a second collection over ``match_all`` across every
    # index in the search context. On an unscoped search that is entities +
    # materials + court cases too (~560k docs in production). Cost is therefore
    # decoupled from selectivity: a query matching nothing walks the whole corpus
    # anyway.
    #
    # Nothing consumes it outside a case view either — the SPA gates the control
    # on ``selectedType === "case"`` — so the widest, most expensive scope was the
    # one whose payload was always discarded. Narrowing to case-only puts the
    # global bucket back on the case index, which is what makes it affordable.
    if _index_for_types(types) == CASE_INDEX:
        aggs["bigo_extent"] = {
            "global": {},
            "aggs": {
                # The AXIS, and now the whole of it: smallest and largest recorded
                # amount plus how many documents carry one. The SPA derives its
                # slider ladder from these three numbers.
                #
                # A ``range`` sub-agg for a distribution histogram used to hang
                # here too. The SPA no longer draws one — the control is a slider
                # over a log ladder — and it was the expensive half: a 14-bucket
                # range agg that re-ran the user's ``multi_match`` across the whole
                # global bucket. ``stats`` on a single numeric field is cheap.
                "stats": {"stats": {"field": "bigo"}},
            },
        }

    body: dict[str, Any] = {
        "size": page_size,
        # Count past OpenSearch's default 10,000-hit cap so ``count`` in the
        # envelope is exact rather than a "gte" lower bound presented as exact.
        "track_total_hits": True,
        # Deterministic order (chosen by ``sort``, always iri-tiebroken) so
        # search_after pages are stable + complete.
        "sort": _sort_spec(sort),
        "query": {"bool": bool_query},
        # Highlight the title/body so the envelope can carry a snippet. (Harmless
        # in browse mode — there's no matched term to highlight.)
        "highlight": {
            "fields": {
                "title_ne": {},
                "title_en": {},
                "body": {},
            }
        },
        "aggs": aggs,
    }

    # Did-you-mean vocabulary lookup (design §11), on the SAME request — no extra
    # round trip, and ``suggest_mode: missing`` makes it near-free when the query is
    # spelled correctly. Gated on the same eligibility as the fuzzy route above, so
    # a Devanagari or identifier query carries no ``suggest`` key at all.
    #
    # It is requested unconditionally for an eligible query rather than only for a
    # zero-hit one — whether the response USES it is decided in
    # ``SearchService.search``, which is the only place the hit count is known.
    if fuzzy_tokens:
        body["suggest"] = _suggest_block(fuzzy_tokens)

    indices_boost = _indices_boost()
    if indices_boost:
        body["indices_boost"] = indices_boost

    if search_after is not None:
        # Cursor paging: resume after the previous page's last hit. ``from`` is
        # omitted (search_after + from is invalid); ``page`` is ignored here.
        body["search_after"] = search_after
    else:
        body["from"] = (page - 1) * page_size

    return body


# Corpus @id IRIs are minted as ``https://<host><marker><tail>`` where ``<marker>``
# is the type's leading path segment and ``<tail>`` is the type-specific remainder:
#   entity    -> /entity/<prefix>/<slug>      (e.g. organization/education/campus/…)
#   material  -> /material/<source>/<ident>
#   courtcase -> /courtcase/<court>/<case_number>
# The frontend mounts a splat route at each ``<marker>`` and resolves the same
# ``<tail>``. We emit a SAME-ORIGIN relative path (not the absolute IRI) so the link
# works through the SPA router rather than 404ing as ``/material/https://...``.
_ENTITY_IRI_MARKER = "/entity/"
_MATERIAL_IRI_MARKER = "/material/"
_COURTCASE_IRI_MARKER = "/courtcase/"


def _iri_relative_path(iri: str | None, marker: str) -> str | None:
    """Same-origin SPA path from an IRI: ``<marker><tail>`` (or None).

    Strips the scheme+host so the link resolves through the SPA router. Two
    marker-miss cases are handled distinctly:
      * a bare tail (no scheme, e.g. ``person/deuba``) is prefixed with ``marker``
        (``/entity/person/deuba``) — the indexers sometimes store the bare tail;
      * a full absolute URL with NO marker (malformed / foreign host) is returned
        UNCHANGED — never re-prefixed into the ``/material/https://...`` 404 this
        helper exists to avoid.
    """
    if not iri:
        return None
    idx = iri.find(marker)
    if idx != -1:
        tail = iri[idx + len(marker) :]
        return f"{marker}{tail}" if tail else None
    # No marker: a scheme'd absolute URL is foreign/malformed → leave it alone; a
    # bare tail gets the marker prefix.
    if "://" in iri:
        return iri
    return f"{marker}{iri}" if iri else None


def _entity_frontend_path(iri: str | None) -> str | None:
    """Relative SPA path for an entity IRI: ``/entity/<tail>`` (or None)."""
    return _iri_relative_path(iri, _ENTITY_IRI_MARKER)


def _frontend_url(result_type: str, source: dict[str, Any]) -> str | None:
    """Best-effort public frontend URL for a hit (mirrors the old envelope)."""
    iri = source.get("iri")
    raw = source.get("raw") or {}
    if result_type == "case":
        slug = raw.get("slug")
        return f"/case/{slug}" if slug else iri
    if result_type == "entity":
        return _entity_frontend_path(iri)
    if result_type == "material":
        return _iri_relative_path(iri, _MATERIAL_IRI_MARKER)
    if result_type == "courtcase":
        return _iri_relative_path(iri, _COURTCASE_IRI_MARKER)
    return iri


def _api_url(result_type: str, source: dict[str, Any]) -> str | None:
    """Owning-app detail API URL for a hit (clients follow it for the full record)."""
    raw = source.get("raw") or {}
    if result_type == "case":
        slug = raw.get("slug")
        return f"/api/cases/{slug}/" if slug else None
    if result_type == "entity":
        return None  # entities are resolved by IRI in-process; no public detail API
    if result_type == "material":
        return None
    if result_type == "courtcase":
        court = raw.get("court")
        number = raw.get("case_number")
        # Composite-key detail route (courts.urls): the case sub-tree is
        # ``cases/<court>/<case_number>`` mounted at /api/ — NOT nested under
        # ``courts/`` (that router only serves the bare /courts list).
        if court and number:
            return f"/api/courtcases/{court}/{number}/"
        return None
    return None


def _snippet(highlight: dict[str, Any]) -> dict[str, str]:
    """Turn the OpenSearch highlight block into a bilingual snippet object."""
    snippet: dict[str, str] = {}
    if not highlight:
        return snippet
    if highlight.get("title_ne"):
        snippet["ne"] = " … ".join(highlight["title_ne"])
    if highlight.get("title_en"):
        snippet["en"] = " … ".join(highlight["title_en"])
    if "ne" not in snippet and "en" not in snippet and highlight.get("body"):
        # Body is mixed-script; expose it on both sides as a fallback excerpt.
        body_excerpt = " … ".join(highlight["body"])
        snippet["ne"] = body_excerpt
        snippet["en"] = body_excerpt
    return snippet


def _serialize_hit(hit: dict[str, Any]) -> dict[str, Any]:
    """One OpenSearch hit → the common result envelope."""
    source = hit.get("_source") or {}
    index = hit.get("_index", "")
    result_type = type_for_index(index) or source.get("source_app", "unknown")
    highlight = hit.get("highlight") or {}

    extra: dict[str, Any] = {}
    # ``weight`` is here so a ``sort=featured`` response explains its own order;
    # absent from docs indexed before the field existed, hence the None guard.
    for key in (
        "date",
        "date_bs",
        "type",
        "weight",
        "court_type",
        "court_district",
        "court_province",
    ):
        if source.get(key) is not None:
            extra[key] = source[key]
    raw = source.get("raw") or {}
    # ``court`` stays sourced from ``raw`` even though it is now ALSO a top-level
    # indexed field: raw carries it on every court-case doc ever written, so
    # ``extra.court`` keeps working on docs indexed before the top-level field
    # existed (i.e. before the --rebuild this change needs). One source, no
    # precedence question, no window where the response loses the court.
    for key in ("case_type", "case_status", "court", "case_number"):
        if raw.get(key) is not None:
            extra[key] = raw[key]

    envelope: dict[str, Any] = {
        "type": result_type,
        "id": source.get("iri"),
        "source_app": source.get("source_app"),
        "title": {
            "ne": source.get("title_ne"),
            "en": source.get("title_en"),
        },
        "snippet": _snippet(highlight),
        "score": hit.get("_score"),
        "url": _frontend_url(result_type, source),
        "api_url": _api_url(result_type, source),
        "matched_fields": sorted(highlight.keys()),
        "extra": extra,
    }

    # Case hits carry a denormalized ``card`` payload (indexed under ``raw.card``)
    # so the SPA renders the result card without a follow-up /api/cases/{slug}/
    # fetch. Only cases have it; older docs indexed before this field simply omit
    # it, so the key is absent rather than null.
    if result_type == "case":
        card = raw.get("card")
        if card is not None:
            envelope["card"] = card

    return envelope


def _facets_from_aggs(aggs: dict[str, Any]) -> dict[str, int]:
    """Per-type counts from the ``by_index`` aggregation (type → doc count)."""
    counts: dict[str, int] = {}
    buckets = (aggs.get("by_index") or {}).get("buckets") or []
    for bucket in buckets:
        # Buckets are keyed by the CONCRETE backing index, not the alias we
        # queried — so this needs the same generation-aware resolution as a hit.
        result_type = type_for_index(bucket.get("key") or "")
        if result_type:
            counts[result_type] = counts.get(result_type, 0) + bucket.get(
                "doc_count", 0
            )
    return counts


def _suggested_replacements(suggest: Any) -> dict[str, str]:
    """Best correction per misspelled token: ``{typed_token: replacement}``.

    Candidates are ranked by **(field authority, score, freq)** — authority FIRST,
    which is the whole point. Authority is position in :data:`SUGGEST_FIELDS`, and
    ranking on score alone measurably surfaces junk: for ``melamchee`` the
    machine-romanized ``title_translit`` offers ``maramchee`` at 0.78 while the
    curated ``keywords.text`` offers ``melamchi`` at 0.75. Frequency cannot rescue
    that either — every candidate there comes back ``freq: 1`` — so a
    noisy-channel ``score x log(freq)`` prior still picks the wrong one. Design
    §11 says to suggest from indexed titles AND "approved aliases"; when the two
    disagree, the human-curated vocabulary is the one to trust.

    ``freq`` stays in the key as a last tiebreak: within one field it is a genuine
    ``P(correction)`` prior, so the commoner of two equally-close candidates wins.

    Empty dict when nothing was suggested or the block is absent/malformed —
    parsing is defensive at every level because this rides on the happy path of a
    successful search and must never turn one into a 500.
    """
    if not isinstance(suggest, dict):
        return {}
    # token -> (rank_key, replacement); the highest rank_key per token wins.
    best: dict[str, tuple[tuple[float, float, float], str]] = {}
    for name, entries in suggest.items():
        if not isinstance(entries, list):
            continue
        # Negated so that position 0 (most authoritative) sorts HIGHEST. An entry
        # under an unknown key ranks below every declared field rather than
        # winning by accident.
        authority = (
            -float(SUGGEST_FIELDS.index(name))
            if name in SUGGEST_FIELDS
            else -float(len(SUGGEST_FIELDS))
        )
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            token = entry.get("text")
            options = entry.get("options")
            if not isinstance(token, str) or not isinstance(options, list):
                continue
            for option in options:
                if not isinstance(option, dict):
                    continue
                replacement = option.get("text")
                if not isinstance(replacement, str) or not replacement:
                    continue
                raw_score = option.get("score")
                score = float(raw_score) if isinstance(raw_score, (int, float)) else 0.0
                raw_freq = option.get("freq")
                freq = float(raw_freq) if isinstance(raw_freq, (int, float)) else 0.0
                rank = (authority, score, freq)
                current = best.get(token)
                if current is None or rank > current[0]:
                    best[token] = (rank, replacement)
    return {token: replacement for token, (_rank, replacement) in best.items()}


def _apply_replacements(q: str, replacements: dict[str, str]) -> str | None:
    """The corrected query string, or ``None`` if there is nothing to offer.

    Rebuilt from ``normalize_query(q).split()``, NOT from the eligible tokens: a
    mixed query must keep the terms the suggester never looked at. ``bhrastachar
    2081`` suggesting ``bhrashtacar`` becomes ``bhrashtacar 2081``, not a bare
    ``bhrashtacar`` that quietly widens what the reader asked for.

    ``None`` when nothing was suggested or when the rebuilt string equals the
    input — a suggestion identical to the query is not a suggestion.
    """
    if not replacements:
        return None
    normalized = normalize_query(q)
    suggestion = " ".join(
        replacements.get(token, token) for token in normalized.split()
    )
    if not suggestion or suggestion == normalized:
        return None
    return suggestion


def _did_you_mean_from_suggest(q: str, suggest: Any) -> str | None:
    """A single corrected query string from a raw ``suggest`` block, or ``None``.

    The two halves composed: rank the candidates, then substitute. Kept as one
    entry point because ``SearchService.search`` needs the ranked replacements on
    their own — the weak-match gate is a question about WHICH tokens were
    corrected, not about the final string.
    """
    return _apply_replacements(q, _suggested_replacements(suggest))


def _extents_from_aggs(aggs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Range-filter EXTENT from the ``global`` extent agg, keyed by request param
    prefix (``bigo`` covers ``bigo_min``/``bigo_max``).

    ``{}`` when the agg was not requested (no case-only scope) — and the entry is
    omitted when the corpus holds no recorded amount at all, in which case
    ``stats`` reports ``count: 0`` with null bounds. A caller must treat an absent
    extent as "no control to render" rather than as a zero-width range.

    Bounds come back from ``stats`` as JSON doubles and are cast to ``int``: the
    corpus already reaches the tens of अरब, and a float would lose precision past
    2**53. The SPA derives its slider ladder from these three numbers, so there is
    nothing here for a client to reinvent and nothing to keep in step.
    """
    extent = aggs.get("bigo_extent") or {}
    stats = extent.get("stats") or {}
    if not stats.get("count") or stats.get("min") is None or stats.get("max") is None:
        return {}
    return {
        "bigo": {
            "min": int(stats["min"]),
            "max": int(stats["max"]),
            "count": int(stats["count"]),
        }
    }


def _named_facets_from_aggs(aggs: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The exposed refine facets (entity_type/case_type/tags) as ``{name, count}``
    lists from their ``terms`` aggregations. Display names are derived client-side.
    """
    facets: dict[str, list[dict[str, Any]]] = {}
    for param in FACET_FIELDS:
        buckets = (aggs.get(param) or {}).get("buckets") or []
        facets[param] = [
            {"name": b.get("key"), "count": b.get("doc_count", 0)}
            for b in buckets
            if b.get("key") is not None
        ]
    return facets


class SearchService:
    """Run the unified multi-index query and serialize the common envelope."""

    def __init__(self, client=None):
        # ``client`` is injectable for tests; defaults to the env-configured one.
        self._client = client

    def _get_client(self):
        if self._client is None:
            self._client = make_client()
        return self._client

    def search(
        self,
        *,
        q: str,
        types: list[str] | None = None,
        lang: str = "both",
        sort: str = SORT_RELEVANCE,
        filters: dict[str, list[str]] | None = None,
        ranges: dict[str, Any] | None = None,
        facet_queries: dict[str, str] | None = None,
        page: int = 1,
        page_size: int = 10,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Execute the unified search and return the response envelope.

        Two paging modes (see :func:`build_query`):

        * **offset** (default): ``page``/``page_size``. Cheap, supports jump-to-page,
          but bounded — requesting an offset past
          :data:`MAX_OFFSET_RESULT_WINDOW` raises :class:`SearchError` (→ 400) with
          guidance to switch to the cursor.
        * **cursor** (``cursor=...``): unbounded deep paging via ``search_after``.
          ``page`` is ignored; the response carries a ``next_cursor`` (null on the
          last page) to fetch the following page.

        The envelope also carries ``did_you_mean``: a spelling suggestion from the
        suggester requested on the same OpenSearch call by :func:`build_query`.
        Non-null when that suggester offered a correction AND either the search
        returned nothing or every fuzzy-eligible token was corrected (design §11's
        two triggers — see the gate below). Always present, never applied
        automatically.

        Raises :class:`SearchUnavailable` if the cluster can't be reached (→ 503)
        and :class:`SearchError` on a bad cursor / over-deep offset (→ 400).
        """
        page = max(1, page)
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))

        search_after = decode_cursor(cursor) if cursor else None
        if search_after is None and page * page_size > MAX_OFFSET_RESULT_WINDOW:
            raise SearchError(
                f"offset paging is limited to {MAX_OFFSET_RESULT_WINDOW} results "
                "(page * page_size); use the 'cursor' from the previous response "
                "for deeper paging."
            )

        body = build_query(
            q=q,
            types=types,
            lang=lang,
            sort=sort,
            filters=filters,
            ranges=ranges,
            facet_queries=facet_queries,
            page=page,
            page_size=page_size,
            search_after=search_after,
        )
        index = _index_for_types(types)

        client = self._get_client()
        try:
            response = client.search(index=index, body=body)
        except Exception as exc:  # noqa: BLE001 — any transport failure is a 503.
            logger.warning("unified search query failed", exc_info=True)
            raise SearchUnavailable(str(exc)) from exc

        hits_block = response.get("hits") or {}
        hit_list = hits_block.get("hits") or []
        results = [_serialize_hit(h) for h in hit_list]

        total = hits_block.get("total")
        count = total.get("value", 0) if isinstance(total, dict) else total or 0

        aggregations = response.get("aggregations") or {}
        counts = _facets_from_aggs(aggregations)
        facets = _named_facets_from_aggs(aggregations)
        extents = _extents_from_aggs(aggregations)

        # next_cursor is the last hit's sort values — present only when the page
        # was full (a short page means there is nothing after it).
        next_cursor: str | None = None
        if hit_list and len(hit_list) == page_size:
            last_sort = hit_list[-1].get("sort")
            if last_sort:
                next_cursor = encode_cursor(last_sort)

        # Did-you-mean (design §11): offered on EITHER of the spec's two triggers —
        # ``result_count == 0``, or a result set holding "only weak matches". The
        # key is always present (like ``next_cursor``) so a client can read it
        # without probing the shape, and the suggestion is never applied for the
        # reader: it is an offer, not a rewrite.
        #
        # The weak-match half is not a score threshold (BM25 scores are not
        # comparable across queries, so any cutoff would be a magic number). It
        # falls out of ``suggest_mode: "missing"`` for free: the suggester only
        # returns options for terms ABSENT from the index, so a corrected token is
        # provably not matched exactly by anything in the result set. When EVERY
        # eligible token was corrected, the whole result set is fuzzy — there is no
        # exact anchor — and that is precisely "only weak matches".
        #
        # This half matters more than the zero-result half, and gating on
        # ``count == 0`` alone made the feature nearly unreachable: bounded fuzzy
        # matching now rescues most misspellings, so the queries that most need a
        # spelling hint (``coruption`` -> 199 hits, ``bhrastachar`` -> 1507) stopped
        # qualifying the moment §10 landed. The two features would have
        # cannibalized each other.
        #
        # Requiring ALL eligible tokens to be corrected is what keeps it quiet on a
        # healthy search: in ``corruption coruption`` the first token IS indexed, so
        # the results have a real anchor and no suggestion is offered. The known
        # rough edge is a mixed-script query — an ineligible Devanagari token can
        # anchor strong results while the lone Roman token still triggers the offer.
        # That reads as OpenSearch's own "including results for" behaviour rather
        # than as a wrong answer, so v1 accepts it.
        did_you_mean: str | None = None
        if q and q.strip():
            replacements = _suggested_replacements(response.get("suggest"))
            eligible = fuzzy_eligible_tokens(q)
            only_weak_matches = bool(eligible) and all(
                token in replacements for token in eligible
            )
            if count == 0 or only_weak_matches:
                did_you_mean = _apply_replacements(q, replacements)

        return {
            "query": q,
            "normalized_query": normalize_query(q),
            "lang": lang,
            "sort": sort,
            "page": page,
            "page_size": page_size,
            "count": count,
            "counts": counts,
            "facets": facets,
            # Corpus extent for the range filters, distinct from ``facets``
            # (which are term buckets). Empty unless the search is case-only.
            "extents": extents,
            "results": results,
            "next_cursor": next_cursor,
            # Null unless the suggester found a correction AND the result set is
            # empty or wholly fuzzy. Never applied automatically — the client
            # re-searches only if the reader picks it.
            "did_you_mean": did_you_mean,
        }
