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
}


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
       (cases/entities) above raw materials at near-equal textual score.

    Paging: a deterministic :data:`SORT_SPEC` (score desc, ``iri`` asc tiebreaker)
    is ALWAYS applied so results are stable and cursorable. When ``search_after``
    is given (the previous page's last-hit sort values), ``from`` is omitted and
    OpenSearch resumes after that point — unbounded deep paging with no
    ``max_result_window`` ceiling. Otherwise shallow offset (``from``/``size``)
    paging is used.

    Per-type facet counts come from a ``_index`` terms aggregation (one index per
    type — exact regardless of ``source_app``, which is not 1:1 with type since
    ngm owns both materials and courtcases).
    """
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))

    # Exact-match facet filters (entity_type/case_type/tags) compose with the text
    # query as bool ``filter`` clauses (no scoring impact, just narrowing).
    filter_clauses: list[dict[str, Any]] = []
    for param, values in (filters or {}).items():
        field = FACET_FIELDS.get(param)
        if field and values:
            filter_clauses.append({"terms": {field: list(values)}})

    # ``q`` is OPTIONAL. With a term, build the tuned recall+precision bool query;
    # with an empty/blank ``q`` it's a BROWSE — ``match_all`` so the facet filters,
    # type selection, sort and paging still apply (list/page the corpus with no
    # search term). An empty multi_match would match nothing, so we must branch.
    has_query = bool(q and q.strip())
    if has_query:
        bool_query: dict[str, Any] = {
            # Recall clause: at least one of the bilingual fields must match.
            "must": [
                {
                    "multi_match": {
                        "query": q,
                        "fields": _weighted_query_fields(lang),
                        "type": "most_fields",
                        "operator": "or",
                    }
                }
            ],
            # Precision boost: adjacent-term (phrase) title match adds score but is
            # not required, so single-term queries still match.
            "should": [
                {
                    "multi_match": {
                        "query": q,
                        "fields": PHRASE_FIELDS,
                        "type": "phrase",
                        "boost": PHRASE_BOOST,
                    }
                }
            ],
            # Exact-match facet narrowing (omitted when no filters requested).
            "filter": filter_clauses,
        }
    else:
        # Browse mode: match everything (optionally narrowed by facet filters),
        # ordered by ``sort`` (relevance is uniform here, so a date/title sort is
        # the useful default for browsing — the caller picks via ``sort``).
        bool_query = {
            "must": [{"match_all": {}}],
            "filter": filter_clauses,
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
        # Aggregations: per-type ``counts`` (by physical index) PLUS the exposed
        # facets (entity_type via the schema.org ``type`` token, case_type, and
        # tags via ``keywords``). Facet counts reflect the query but NOT the facet
        # filters (OpenSearch aggregates over the post-filter result set; that's the
        # accepted behaviour for these refine-style facets).
        "aggs": {
            # Sized 2x the type count, not 1x: the bucket key is the CONCRETE
            # backing index, and mid-swap an alias can briefly resolve to two
            # generations. At exactly len(ALL_TYPES) the extra bucket would push
            # a real one out and silently zero that type's facet count.
            "by_index": {"terms": {"field": "_index", "size": 2 * len(ALL_TYPES)}},
            "entity_type": {"terms": {"field": "type", "size": 50}},
            "case_type": {"terms": {"field": "case_type", "size": 50}},
            "tags": {"terms": {"field": "keywords", "size": 50}},
            "status": {"terms": {"field": "case_status", "size": 50}},
        },
    }

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
    for key in ("date", "date_bs", "type", "weight"):
        if source.get(key) is not None:
            extra[key] = source[key]
    raw = source.get("raw") or {}
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

        # next_cursor is the last hit's sort values — present only when the page
        # was full (a short page means there is nothing after it).
        next_cursor: str | None = None
        if hit_list and len(hit_list) == page_size:
            last_sort = hit_list[-1].get("sort")
            if last_sort:
                next_cursor = encode_cursor(last_sort)

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
            "results": results,
            "next_cursor": next_cursor,
        }
