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

# How many eligible tokens one query may fuzz, and how far each may expand. Both
# bound COST, and the reason they exist is that ``q`` is a plain unbounded
# ``CharField`` — the serializer sets no max length, so query size is attacker- (or
# accident-) controlled.
#
# That was affordable while every token was matched exactly. Fuzziness changes the
# shape of the work: each term walks the term dictionary for neighbours within two
# edits, across four fields. Left unbounded, a pathological query multiplies out to
# a very large disjunction, and past
# ``indices.query.bool.max_clause_count`` OpenSearch rejects it outright — which
# this service turns into a 503, so an over-long query degrades AVAILABILITY rather
# than just being slow.
#
# The cap TRUNCATES rather than disabling the route: a real query needing a
# spelling rescue is a handful of words, so the first tokens are the ones that
# matter, and a 40-word paste keeps exact recall on all of it either way.
FUZZY_MAX_TOKENS = 12
# Neighbours per term. 50 is also OpenSearch's default; stated explicitly because
# "bounded" is the whole promise of this route and a default is not a decision.
FUZZY_MAX_EXPANSIONS = 50

# Named queries on the two recall routes. OpenSearch echoes the names that matched
# on each hit as ``matched_queries``, which is how ``search`` tells a fuzzy rescue
# apart from a genuine match WITHOUT inferring it — see
# :func:`_result_set_is_wholly_fuzzy`. Emitted ONLY when the fuzzy route is active,
# so the no-op guarantee (byte-identical DSL for an ineligible query) still holds.
EXACT_RECALL_CLAUSE_NAME = "exact_recall"
FUZZY_RECALL_CLAUSE_NAME = "fuzzy_recall"

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
    "material_type": "material_type",
}

# The closed vocabulary behind the ``material_type`` facet — every
# ``Material.material_type`` token.
#
# A LITERAL, not a comprehension over ``MaterialType``, for the same reason
# ``ALL_COURT_TYPES`` above is one: three consumers must agree (the
# serializer's ``ChoiceField``, which is what 400s; the OpenAPI enum, which is
# what the SPA reads; the MCP tool's schema, which is what a model reads), and
# ``search/service.py`` deliberately imports from no sibling app. Pinned to
# ``materials.jsonld.MaterialType`` by
# ``test_material_type_enum_tracks_material_types`` — add a type there and that
# test fails here until this list catches up.
ALL_MATERIAL_TYPES: tuple[str, ...] = (
    "charge_sheet",
    "court_case",
    "court_order",
    "document",
    "legal_corpus",
    "manuscript",
    "news",
    "official_report",
    "precedent",
    "press_release",
    "procurement_notice",
    "social_media",
)

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

# SCOPE fields: exact-match narrowing that is NOT a facet. Request param name ->
# the keyword index field it filters.
#
# A facet (:data:`FACET_FIELDS`) is a list of options offered BACK to the reader,
# so it carries an aggregation, and its clause goes to ``post_filter`` so the
# facet does not narrow itself. A scope is the opposite: the caller already knows
# the value, nothing enumerates it, and it defines WHICH CORPUS is being searched
# — so it belongs in the query's ``bool.filter`` and narrows the hits and every
# aggregation alike, exactly like the range bounds.
#
# ``source`` is the motivating case. The /materials series registry is 1:1 with
# ``Material.source``, so "search inside the CIAA annual reports" is
# ``?type=material&source=ciaa_annual_report&q=…``. Faceting that column would be
# wrong — a third of its tokens restate the document form rather than naming a
# publisher — but scoping to a token the registry already picked is not the same
# act. ``material_type`` remains the facet that IS safe to show.
# ``dataset_bucket`` is the second, and exists because ``source`` is too coarse to
# shelve by: all 228 Auditor General documents share the token ``official_report``
# and only 18 are annual reports — the rest are province audit reports, the audit
# journal, audit bulletins and nine other kinds. It is emphatically NOT a facet,
# for a sharper version of the same reason as ``source``: the vocabulary is the
# upstream dataset's raw slugified column, and
# ``publication_auditor-general's--work-achievement`` is not a label to hand a
# reader. The registry picks the token; nothing enumerates the list back.
SCOPE_FIELDS: dict[str, str] = {
    "source": "source",
    "dataset_bucket": "dataset_bucket",
}

# NEGATIVE scopes: same registry discipline, ``must_not`` instead of ``must``.
# Request param name -> the keyword index field it excludes.
#
# This exists for exactly one shape: a curated shelf that means "everything in
# this source that the other shelves did not claim". Enumerating the dozen-odd
# remaining tokens in the frontend registry would work until the upstream corpus
# mints a new one — and then those documents belong to no shelf at all, silently.
# Stated as a complement, the shelves are a provable partition of whatever the
# source actually contains, and a new token lands in "other" instead of nowhere.
#
# Repeated values union into one ``must_not``, so excluding four tokens is four
# values on one param, not four params.
EXCLUDE_SCOPE_FIELDS: dict[str, str] = {
    "dataset_bucket_exclude": "dataset_bucket",
}

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
    # No entry for ``material_type``: 12 tokens against a default of 50, the
    # same reason ``court_type``'s four tiers have none.
}

# Sub-agg name holding the bucket list when a facet agg is WRAPPED in its
# self-excluding ``filter`` agg (see ``_scoped``). A fixed string rather than the
# param name so the response reader walks one path per shape, not one per facet.
FACET_AGG_INNER = "values"

# Suffix of the companion agg that pins the caller's OWN selected values into a
# widened facet's bucket list.
#
# Needed only because the facet is now widened: before, a selected value was the
# ONLY bucket and so always present. A widened facet is a top-N over a much larger
# term space (``case_type`` has 2,332 distinct values in the NGM docket against a
# size of 50), so a rare selection can now fall out of its own facet, and the
# reader would lose the bucket it has ticked. This agg carries its real count back.
#
# Scope of the guarantee: it restores a selection the top-N DROPPED. A selection
# that matches no document in the widened scope has no bucket to carry back, so it
# is still absent from the list — rendering a ticked-but-absent value as zero is
# the client's job, not something this agg can express.
FACET_SELECTED_SUFFIX = "__selected"

# Agg names that are NOT FACET_FIELDS facets, and so must stay distinguishable
# from one (see the ``aggs`` dict in ``build_query``).
_RESERVED_AGG_NAMES = frozenset({"by_index", "bigo_extent"})


def _assert_agg_namespace_is_unambiguous() -> None:
    """The facet aggs, their ``__selected`` companions and the hand-written aggs
    all share ONE namespace in the request body, and the response readers resolve
    a bucket list by name alone.

    A param whose name collided with another param's companion would therefore
    silently shadow it — the reader would hand one facet's buckets back as
    another's, with no error anywhere. Checked at import against the registry
    rather than left to review, in the same spirit as generating the aggs off
    FACET_FIELDS: a facet param cannot exist without its aggregation, and now it
    cannot exist under a name that steals someone else's either.
    """
    for param in FACET_FIELDS:
        companion = param + FACET_SELECTED_SUFFIX
        clash = (
            "a facet param"
            if companion in FACET_FIELDS
            else "a reserved agg" if companion in _RESERVED_AGG_NAMES else None
        )
        if clash:
            raise RuntimeError(
                f"facet param {param!r} needs the agg name {companion!r}, which is "
                f"already {clash}. Rename one of them: the response readers resolve "
                f"buckets by agg name, so the collision would silently swap them."
            )
        if param in _RESERVED_AGG_NAMES:
            raise RuntimeError(
                f"facet param {param!r} collides with the hand-written agg of the "
                f"same name; rename the facet."
            )


_assert_agg_namespace_is_unambiguous()


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


def _scope_clauses(scopes: dict[str, list[str]] | None) -> list[dict[str, Any]]:
    """``terms`` clauses for the corpus scopes (:data:`SCOPE_FIELDS`).

    Iterates the registry rather than the caller's dict, for the same two reasons
    :func:`_range_clauses` does: unknown params are ignored, and the emitted DSL
    is byte-stable regardless of query-string order.

    Repeated values union (``?source=a&source=b`` is either), matching how the
    facet params behave; different scopes AND with each other.

    :data:`EXCLUDE_SCOPE_FIELDS` emits the negation of the same clause. The
    ``bool.must_not`` is nested inside the ``filter`` array rather than hoisted
    to the query's own ``must_not``, which keeps it in filter context (no
    scoring, cacheable) and keeps this function's contract intact: it returns a
    list of filter clauses and the caller appends them, unchanged.

    A document with NO value for the field is NOT excluded by ``must_not`` —
    which is the behaviour a complement shelf wants (the one Auditor General row
    carrying no bucket belongs in "other", not nowhere), and the mirror of the
    positive clause excluding it.
    """
    clauses: list[dict[str, Any]] = []
    for param, field in SCOPE_FIELDS.items():
        values = (scopes or {}).get(param)
        if values:
            clauses.append({"terms": {field: list(values)}})
    for param, field in EXCLUDE_SCOPE_FIELDS.items():
        values = (scopes or {}).get(param)
        if values:
            clauses.append({"bool": {"must_not": {"terms": {field: list(values)}}}})
    return clauses


def _scoped(
    agg: dict[str, Any], others: list[dict[str, Any]]
) -> dict[str, Any]:
    """``agg`` re-narrowed by ``others`` — the terms filters ``post_filter`` took
    out of the query context.

    An EMPTY ``others`` returns ``agg`` UNWRAPPED, which is what makes the common
    cases free: a request with no terms filter at all emits exactly the pre-
    ``post_filter`` DSL, and a facet that is the only one filtered keeps its flat
    agg (now evaluated in the widened context — that IS the fix).
    """
    if not others:
        return agg
    return {"filter": {"bool": {"filter": others}}, "aggs": {FACET_AGG_INNER: agg}}


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

    Truncated to :data:`FUZZY_MAX_TOKENS`. Both consumers — the fuzzy clause and
    the suggester — read this ONE list, so the bound applies to each of them and
    they cannot disagree about which tokens are in play.
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
        if len(tokens) == FUZZY_MAX_TOKENS:
            break
    return tokens


def _fuzzy_clause(tokens: list[str]) -> dict[str, Any]:
    """The damped fuzzy recall ``multi_match`` for the eligible tokens.

    Only the ELIGIBLE tokens are queried, not the raw ``q``: passing the whole
    string back would re-admit the Devanagari/identifier terms that eligibility
    just excluded, and ``fuzziness`` applies per term.

    ``most_fields`` (not ``cross_fields``) because cross_fields silently DROPS
    fuzziness — see ``docs/shared/research/opensearch-bilingual-nepali.md`` §5.

    Carries ``_name`` so each hit reports whether it arrived via this route; the
    did-you-mean gate reads that rather than guessing.
    """
    return {
        "multi_match": {
            "query": " ".join(tokens),
            "fields": FUZZY_FIELDS,
            "type": "most_fields",
            "operator": "or",
            "fuzziness": FUZZINESS,
            "prefix_length": FUZZY_PREFIX_LENGTH,
            "max_expansions": FUZZY_MAX_EXPANSIONS,
            "boost": FUZZY_BOOST,
            "_name": FUZZY_RECALL_CLAUSE_NAME,
        }
    }


def _suggest_block(tokens: list[str]) -> dict[str, Any]:
    """The ``suggest`` request block feeding ``did_you_mean`` (design §11).

    One ``term`` entry per :data:`SUGGEST_FIELDS`, both over the same ``text`` (the
    eligible tokens). Entries are keyed by their field name so the response parser
    needs no separate name→field map.

    No ``collate`` in v1, but the reason is narrower than "it cannot miss". The
    vocabulary comes from fields the query already searches, so an UNFILTERED
    search on the suggestion does find something. That is not a guarantee for the
    request actually made: suggestions are drawn from whole-field term
    dictionaries, while the query also carries ``bool.filter`` clauses (type,
    facets, ``bigo`` range, court/district), and none of those constrain what the
    suggester offers. With a filter active a suggestion can therefore lead to a
    second empty state — the one thing this feature exists to get readers out of.

    Accepted for v1 because the failure is self-correcting (the reader sees an
    empty result and can widen the filter) and because ``collate`` costs a query
    per candidate. If analytics show suggestions landing on empty filtered pages,
    ``collate`` with the live filter clauses is the fix.
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
    scopes: dict[str, list[str]] | None = None,
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

    # Exact-match facet filters (entity_type/case_type/tags/…). These NO LONGER
    # live in the main query's bool ``filter``: they are emitted as a top-level
    # ``post_filter`` (applied to the hits after the aggregations are collected)
    # plus a per-facet ``filter`` agg re-applying every filter EXCEPT that facet's
    # own. That is what stops a facet being narrowed by its own selection — see
    # the aggs comment below for the whole argument.
    #
    # Kept KEYED BY PARAM so exactly one clause can be dropped per facet, and
    # built by iterating FACET_FIELDS rather than the caller's dict so the emitted
    # body is byte-stable regardless of query-string order — the same reasoning
    # ``_range_clauses`` states, and it matters more here because the clause list
    # is now repeated up to eleven times in one body.
    terms_by_param: dict[str, dict[str, Any]] = {}
    for param, field in FACET_FIELDS.items():
        values = (filters or {}).get(param)
        if values:
            terms_by_param[param] = {"terms": {field: list(values)}}
    terms_clauses: list[dict[str, Any]] = list(terms_by_param.values())
    # Range filters (bigo_min/bigo_max, date_from/date_to) STAY in the query. No
    # facet owns a range, so no facet may drop one; leaving them here means they
    # narrow the hits and every aggregation alike, exactly as before.
    range_clauses = _range_clauses(ranges)
    # Scopes join the RANGE clauses in the query context, not the facet
    # clauses in ``post_filter``: a scope says which corpus is being searched,
    # so the aggregations must see it too — a facet's bucket counts should
    # describe the scoped corpus, not the whole archive.
    scope_clauses = _scope_clauses(scopes)

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
        #
        # Both routes are NAMED, and only on this branch. Each hit then reports the
        # route(s) that matched it as ``matched_queries``, which is what lets the
        # did-you-mean gate in ``SearchService.search`` ASK whether the result set
        # has a genuine (non-fuzzy) anchor instead of inferring it from the
        # suggester. Naming only here keeps the ineligible-query DSL byte-identical.
        named_exact_recall_clause: dict[str, Any] = {
            "multi_match": {
                **exact_recall_clause["multi_match"],
                "_name": EXACT_RECALL_CLAUSE_NAME,
            }
        }
        must_clauses = [
            {
                "bool": {
                    "should": [
                        named_exact_recall_clause,
                        _fuzzy_clause(fuzzy_tokens),
                    ],
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
        # RANGE narrowing only (empty when nothing is requested). The exact-match
        # facet clauses moved to ``post_filter`` — see the aggs comment below.
        "filter": range_clauses + scope_clauses,
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
    # Facet counts reflect the active filters as well as the query, with ONE
    # deliberate exception: a facet does not apply its OWN filter to itself.
    #
    #   - CASCADING is preserved, and callers rely on it: filtering
    #     ``court_type=high`` still empties the ``district`` facet outright,
    #     because no high-court doc carries a ``court_district`` — that empty
    #     bucket list is how a client knows the district refine does not apply to
    #     the current selection. Every facet still applies every OTHER filter, so
    #     this is untouched.
    #   - COLLAPSING is what this shape fixes. A facet used to narrow by its own
    #     filter too, so ticking one document type left ``facets.material_type``
    #     holding a single bucket with no siblings to widen the selection with —
    #     the second box could never be ticked, because the API had already
    #     deleted it. Measured before the fix: ``?type=material`` returned 10
    #     buckets, ``?type=material&material_type=official_report`` returned 1.
    #
    # WHY ``post_filter`` and not a plain ``filter`` sub-agg. Aggregations run
    # inside the query context, so a ``filter`` agg can only ever INTERSECT it —
    # it cannot widen back out to re-expose values the main query already removed.
    # The facet clauses therefore have to leave the query, which is what
    # ``post_filter`` is for: it narrows the hits (and ``hits.total``, so the
    # envelope's ``count`` is unaffected) after the aggs are collected.
    #
    # An EARLIER version of this comment warned that ``post_filter`` "is NOT a
    # substitute, as it would make every facet ignore every filter and so destroy
    # the cascading". That is true of ``post_filter`` ALONE, and it is why the
    # per-facet wrappers below are not optional: each one re-applies every filter
    # except its own, so the only thing any facet stops seeing is itself.
    #
    # Ranges (बिगो, date) deliberately stay in ``bool.filter``: no facet owns a
    # range, so none may drop one, and the range control has its own widening
    # mechanism in the ``global`` ``bigo_extent`` agg below.
    #
    # Built as its own ``dict[str, Any]`` rather than inline in ``body``: the
    # nested literal would otherwise pin a narrow value type that the extent agg
    # below — which nests an ``aggs`` of its own — does not fit.
    aggs: dict[str, Any] = {
        # Sized 2x the type count, not 1x: the bucket key is the CONCRETE
        # backing index, and mid-swap an alias can briefly resolve to two
        # generations. At exactly len(ALL_TYPES) the extra bucket would push
        # a real one out and silently zero that type's facet count.
        #
        # Wrapped with ALL the terms clauses, unlike the refine facets below.
        # ``counts`` is not a refine control — it must keep meaning "the active
        # result set", and it is emitted as the ``counts_by_type`` telemetry
        # series (search/analytics.py). Left unwrapped it would quietly start
        # reporting un-narrowed per-index totals, with no test able to see it.
        "by_index": _scoped(
            {"terms": {"field": "_index", "size": 2 * len(ALL_TYPES)}},
            terms_clauses,
        ),
    }
    # One ``terms`` agg per exposed refine facet, GENERATED from FACET_FIELDS so a
    # facet param can never exist without its aggregation. These used to be
    # hand-listed alongside ``by_index``, which left a trap: a FACET_FIELDS entry
    # with no matching agg here validated fine, filtered fine, and then served an
    # empty ``facets.<param>`` list forever — no error, no log. Driving the aggs
    # off the registry closes that by construction (``by_index`` and the extent
    # agg below stay hand-written: they are not FACET_FIELDS facets).
    for param, field in FACET_FIELDS.items():
        inner: dict[str, Any] = {
            "terms": {"field": field, "size": FACET_AGG_SIZES.get(param, DEFAULT_FACET_AGG_SIZE)}
        }
        # ``facet_q``: recompute ONLY this facet's bucket list to the top buckets
        # whose key contains the text. ``include`` filters the term set BEFORE
        # the size cut, so the match runs over the full aggregation, not the
        # default top-N slice — and it touches nothing but this one agg: the
        # query, count, hits and every other facet are computed exactly as
        # without it. Ordering stays the terms-agg default (count desc).
        #
        # It rides on the INNER terms agg, so it lands in the right place whether
        # or not this facet ends up wrapped. One consequence of the widening: with
        # a filter active on the SAME facet the include now runs over the widened
        # term set — a typeahead over what you could switch to, rather than over
        # the one bucket you already picked.
        text = (facet_queries or {}).get(param)
        if text:
            inner["terms"]["include"] = _facet_include_regex(text)

        # Every OTHER active terms filter. Dropping this facet's own clause is
        # the whole fix; keeping the rest is what preserves the cascading.
        others = [c for p, c in terms_by_param.items() if p != param]
        aggs[param] = _scoped(inner, others)

        # ... and pin the caller's own selection back in. A widened facet is a
        # top-N over a much larger term space than the single bucket it used to
        # return, so a rare selection can now fall out of its own list; without
        # this the reader would lose the very bucket they have ticked.
        # Same ``others`` scope as the widened list, so the two counts agree.
        #
        # It restores a selection the top-N DROPPED — it cannot invent one. A value
        # matching no document in the widened scope has no bucket here either, so it
        # stays absent from ``facets`` and the client decides how to draw a ticked
        # box with nothing behind it.
        #
        # NOT when ``facet_q`` is searching this same facet, though. The pin does
        # not carry the include regex, so it would hand back a bucket that does
        # not contain the typed text — breaking the one thing facet_q promises
        # ("only buckets whose key contains the text"), which the MCP tool states
        # to a model as ground truth. A typeahead asks what you could switch TO;
        # what you are already on is not part of that answer.
        own = terms_by_param.get(param)
        if own is not None and not text:
            selected = own["terms"][field]
            aggs[param + FACET_SELECTED_SUFFIX] = _scoped(
                {
                    # An ARRAY ``include``, never the facet_q regex — a separate
                    # agg, so the two includes cannot collide.
                    "terms": {
                        "field": field,
                        "include": list(selected),
                        "size": len(selected),
                    }
                },
                others,
            )

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

    # The exact-match facet narrowing, applied to the HITS after the aggs above
    # are collected. ``hits.total`` reflects it, so the envelope's ``count`` still
    # counts what the reader is actually looking at. Omitted entirely when nothing
    # is selected, so an unfiltered body is byte-identical to the pre-fix one.
    if terms_clauses:
        body["post_filter"] = {"bool": {"filter": terms_clauses}}

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
    #
    # ``parties`` joins them for the same reason, and is the one entry here that
    # is NOT on every doc: it was added later, so a court case indexed before
    # that only gains it on reindex. The ``is not None`` guard is what makes
    # that a missing key rather than a null, and a client that wants parties on
    # every hit needs the rebuild.
    for key in ("case_type", "case_status", "court", "case_number", "parties"):
        if raw.get(key) is not None:
            extra[key] = raw[key]

    # Case-only, and gated on the doc type rather than copied with the block
    # above, because ``status`` carries TWO vocabularies: Jawafdehi cases write
    # the derived lifecycle, NGM courtcase docs write their scraper enrichment
    # flag (pending/enriched/failed). Copying it unconditionally would publish
    # an internal pipeline state on every court-case hit and give one envelope
    # key two unrelated meanings.
    #
    # ``case_status`` above stays exactly as it was -- it is the legacy
    # three-value facet the deployed SPA badges on, and this adds the full
    # vocabulary alongside so the frontend can migrate on its own schedule.
    #
    # The three stage-era fields are OMITTED by the indexer when unset, so a
    # doc written before the ``reindex_all --rebuild`` this needs simply lacks
    # them; the None guard keeps that a missing key rather than a null.
    if result_type == "case":
        for key in (
            "status",
            "case_track",
            "proceedings_started_on",
            "proceedings_decided_on",
        ):
            if source.get(key) is not None:
                extra[key] = source[key]

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


def _facet_buckets(node: Any) -> list[dict[str, Any]]:
    """Bucket list from a facet agg, whether it came back FLAT or wrapped in the
    self-excluding ``filter`` agg :func:`_scoped` adds.

    Shape-sniffed rather than re-derived from the request: these readers never see
    the request, and sniffing means every canned response that models the flat
    shape keeps parsing unchanged.
    """
    node = node if isinstance(node, dict) else {}
    inner = node.get(FACET_AGG_INNER)
    if isinstance(inner, dict):
        node = inner
    return node.get("buckets") or []


def _facets_from_aggs(aggs: dict[str, Any]) -> dict[str, int]:
    """Per-type counts from the ``by_index`` aggregation (type → doc count)."""
    counts: dict[str, int] = {}
    buckets = _facet_buckets(aggs.get("by_index"))
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

    The two halves composed: rank the candidates, then substitute. This is the ONE
    entry point ``SearchService.search`` uses. It briefly was not: while the
    weak-match gate inferred an anchor from the suggester it needed the ranked
    replacements on their own, so ``search`` called both halves itself and this
    became production code with only tests behind it. Reading the anchor off
    ``matched_queries`` instead retired that need.
    """
    return _apply_replacements(q, _suggested_replacements(suggest))


def _result_set_is_wholly_fuzzy(hits: list[Any]) -> bool:
    """True when every hit on this page arrived ONLY via the damped fuzzy route.

    Design §11's second did-you-mean trigger — "the result set contains only weak
    matches" — MEASURED rather than inferred. :func:`build_query` names both recall
    routes, so each hit reports the route(s) that matched it in ``matched_queries``;
    if no hit carries :data:`EXACT_RECALL_CLAUSE_NAME`, then nothing on the page
    matched what the reader actually typed and the set is a pure fuzzy rescue.

    This REPLACES an inference from the suggester — "every eligible token came back
    corrected, so none of them can be indexed" — which was unsound. ``suggest_mode:
    "missing"`` is evaluated per SUGGEST FIELD, and :data:`SUGGEST_FIELDS` covers
    only ``keywords.text`` and ``title_translit``; the recall route also searches
    ``title_en`` and ``body``. A token living in the OCR ``body`` but absent from
    both suggest fields therefore looked "missing" while matching thousands of
    documents exactly, so a correctly spelled query could draw a spurious
    suggestion. Naming the clauses removes the inference instead of tightening it.

    SCOPE, deliberately: this reads the CURRENT PAGE, since ``matched_queries``
    exists only on returned hits. Under the default relevance sort an exact match
    outranks a damped fuzzy one by construction (:data:`FUZZY_BOOST` sits below
    every exact field weight), so if an anchor exists it surfaces on page one. On a
    non-relevance sort that no longer holds, which is a deliberate v1 limit: the
    consequence is at worst an offered spelling, never a wrong result.

    Returns False whenever the answer cannot be established — no hits, no names
    emitted (the fuzzy route was not active), or a malformed payload. Silence is
    the safe direction on uncertainty: an absent suggestion is invisible, whereas a
    wrong one argues with results already on the reader's screen.
    """
    saw_a_hit = False
    for hit in hits:
        if not isinstance(hit, dict):
            return False
        matched = hit.get("matched_queries")
        # OpenSearch returns a list of names; the dict shape (name -> score) is
        # what comes back when match scores are requested, and its KEYS are the
        # same names. Any other shape means we cannot tell, so we stay quiet.
        if isinstance(matched, dict):
            names: Any = matched.keys()
        elif isinstance(matched, list):
            names = matched
        else:
            return False
        if EXACT_RECALL_CLAUSE_NAME in names:
            return False
        # Neither name present is INCONSISTENT, not evidence of a fuzzy rescue:
        # the two routes sit in a ``minimum_should_match: 1`` bool, so every hit
        # must have matched at least one of them. A payload saying otherwise is
        # not describing the query we sent, so it cannot be reasoned from.
        if FUZZY_RECALL_CLAUSE_NAME not in names:
            return False
        saw_a_hit = True
    return saw_a_hit


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
    lists. Display names are derived client-side.

    Each facet's buckets come from its ``terms`` agg — flat, or nested inside the
    self-excluding ``filter`` agg when some OTHER facet is filtered — merged with
    its ``<param>__selected`` companion, which carries the caller's own selected
    values in case the widened top-N dropped one.
    """
    facets: dict[str, list[dict[str, Any]]] = {}
    for param in FACET_FIELDS:
        by_name: dict[str, int] = {}
        for bucket in _facet_buckets(aggs.get(param)):
            key = bucket.get("key")
            if key is not None:
                by_name[key] = bucket.get("doc_count", 0)
        # ``setdefault``: where both carry a value the widened list wins — it is
        # the same scope, and this one exists only to fill a gap.
        for bucket in _facet_buckets(aggs.get(param + FACET_SELECTED_SUFFIX)):
            key = bucket.get("key")
            if key is not None:
                by_name.setdefault(key, bucket.get("doc_count", 0))
        # Re-sorted count-desc so the MERGED list keeps the ordering the widened
        # one arrived in. A pinned value is one the top-N dropped, so it is almost
        # always the smallest count here and would have sorted last anyway — but
        # "almost always" is not a contract, and clients that truncate the list
        # (the SPA's "More" cut) would quietly drop the wrong bucket the one time
        # it is not. The sort is STABLE, so buckets sharing a count keep
        # OpenSearch's own key ordering rather than being reshuffled.
        facets[param] = [
            {"name": name, "count": count}
            for name, count in sorted(by_name.items(), key=lambda kv: -kv[1])
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
        scopes: dict[str, list[str]] | None = None,
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
        returned nothing or no hit on the page matched the exact recall route
        (design §11's two triggers — see the gate below). Always present, never
        applied automatically.

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
            scopes=scopes,
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
        # comparable across queries, so any cutoff would be a magic number). It is
        # read straight off the hits instead: ``build_query`` NAMES both recall
        # routes, so every hit says whether it matched the reader's actual spelling
        # or only the damped fuzzy rescue. See ``_result_set_is_wholly_fuzzy``,
        # which also records why the earlier suggester-based inference was unsound.
        #
        # This half matters more than the zero-result half, and gating on
        # ``count == 0`` alone made the feature nearly unreachable: bounded fuzzy
        # matching now rescues most misspellings, so the queries that most need a
        # spelling hint (``coruption`` -> 199 hits, ``bhrastachar`` -> 1507) stopped
        # qualifying the moment §10 landed. The two features would have
        # cannibalized each other.
        #
        # Measuring rather than inferring is also what keeps it quiet on a healthy
        # search, and on two searches the inference got wrong: ``corruption
        # coruption`` (the ``corruption`` hits match the exact route, so the page
        # has an anchor) and a mixed-script query like ``देउवा coruption``, where the
        # Devanagari term is fuzzy-INELIGIBLE yet still anchors the result set via
        # the exact route.
        #
        # ``matched_queries`` describes the PAGE, so the weak-match half is only
        # asked when the page can stand in for the result set — the FIRST page of a
        # RELEVANCE-sorted search. Both halves of that matter:
        #
        #   * Not the first page. Relevance puts the exact matches first, so paging
        #     INTO a healthy result set eventually reaches hits the fuzzy route
        #     alone rescued. Without this guard the suggestion would be absent on
        #     page 1 and then appear on page 4 of the same search, which reads as a
        #     glitch rather than an offer.
        #   * Not a relevance sort. Under ``newest``/``oldest``/``title``/
        #     ``featured`` the score does not order anything, so "the exact matches
        #     come first" — the property that lets one page speak for the whole set
        #     — simply does not hold.
        #
        # ``count == 0`` is unaffected: that reads the TOTAL, not the page, and is
        # true regardless of sort or offset.
        #
        # The exact-but-costlier alternative is a ``filter`` agg counting the
        # documents that match the exact route across the whole result set, which
        # would answer this for any page and any sort. It buys a narrow case (a
        # misspelling on a date-sorted search) for an extra aggregation on every
        # fuzzy-eligible query; v2 if the analytics say the case is common.
        page_speaks_for_the_result_set = (
            search_after is None and page == 1 and sort == SORT_RELEVANCE
        )
        did_you_mean: str | None = None
        if q and q.strip() and (
            count == 0
            or (page_speaks_for_the_result_set and _result_set_is_wholly_fuzzy(hit_list))
        ):
            did_you_mean = _did_you_mean_from_suggest(q, response.get("suggest"))

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
