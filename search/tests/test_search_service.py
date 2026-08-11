"""SearchService tests — bilingual query DSL, envelope/merge/facets, 503.

No live cluster: a MagicMock client returns a canned OpenSearch response. Tests
assert the query DSL hits the bilingual fields (incl. ``title_translit``), that
the multi-index hits merge into the common envelope with per-type facet counts,
and that a transport error becomes ``SearchUnavailable`` (→ HTTP 503).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from search import service as svc
from search.service import (
    SearchError,
    SearchService,
    SearchUnavailable,
    build_query,
    decode_cursor,
    encode_cursor,
)


# ── query DSL ──────────────────────────────────────────────────────────────────


def _recall_multi_match(body):
    """The required (recall) multi_match clause from the tuned bool query."""
    return body["query"]["bool"]["must"][0]["multi_match"]


def _phrase_clause(body):
    """The SHOULD exact-phrase title-boost clause from the tuned bool query."""
    return body["query"]["bool"]["should"][0]["multi_match"]


def test_build_query_is_bilingual_and_hits_translit():
    body = build_query(q="शेर बहादुर", page=1, page_size=10)
    fields = _recall_multi_match(body)["fields"]
    # Bilingual: native-script title, roman title, AND the translit bridge field.
    assert any(f.startswith("title_ne") for f in fields)
    assert any(f.startswith("title_en") for f in fields)
    assert any(f.startswith("title_translit") for f in fields), fields
    assert any(f.startswith("body") for f in fields)
    # most_fields strategy (names) per the research config.
    assert _recall_multi_match(body)["type"] == "most_fields"
    # Per-type facet aggregation present.
    assert "by_index" in body["aggs"]
    # Count past OpenSearch's default 10,000-hit cap rather than a "gte" lower
    # bound presented as an exact count.
    assert body["track_total_hits"] is True


def test_build_query_has_exact_phrase_title_boost():
    """An adjacent-term (phrase) title clause floats exact name matches up."""
    body = build_query(q="Sher Bahadur Deuba")
    phrase = _phrase_clause(body)
    assert phrase["type"] == "phrase"
    assert phrase["boost"] == svc.PHRASE_BOOST
    # Phrase clause targets only the title fields (not body).
    assert set(phrase["fields"]) == set(svc.PHRASE_FIELDS)
    assert "body" not in phrase["fields"]


def test_build_query_lang_reweights_title_without_excluding_other():
    """lang is a soft re-rank: the matching script's title boost is multiplied,
    but the other script + translit bridge are still queried (recall preserved)."""
    ne_body = build_query(q="देउवा", lang="ne")
    fields = _recall_multi_match(ne_body)["fields"]
    ne_boost = next(f for f in fields if f.startswith("title_ne^"))
    en_boost = next(f for f in fields if f.startswith("title_en^"))
    # Nepali title outweighs English under lang=ne...
    assert float(ne_boost.split("^")[1]) > float(en_boost.split("^")[1])
    # ...but English + translit are still present (not filtered out).
    assert any(f.startswith("title_en") for f in fields)
    assert any(f.startswith("title_translit") for f in fields)

    en_body = build_query(q="deuba", lang="en")
    en_fields = _recall_multi_match(en_body)["fields"]
    en_b = next(f for f in en_fields if f.startswith("title_en^"))
    ne_b = next(f for f in en_fields if f.startswith("title_ne^"))
    assert float(en_b.split("^")[1]) > float(ne_b.split("^")[1])


def test_build_query_applies_per_type_indices_boost():
    """Primary editorial records (cases/entities) are weighted above raw materials."""
    body = build_query(q="x")
    boosts = {list(d)[0]: list(d.values())[0] for d in body.get("indices_boost", [])}
    # Cases boosted above materials.
    assert boosts["jawafdehi-cases"] > boosts.get("ngm-materials", 1.0)
    # 1.0 (no-op) weights are omitted from the DSL.
    assert "ngm-courtcases" not in boosts  # courtcase weight is exactly 1.0


def test_build_query_paginates():
    body = build_query(q="x", page=3, page_size=10)
    assert body["from"] == 20
    assert body["size"] == 10


def test_build_query_caps_page_size():
    body = build_query(q="x", page=1, page_size=10_000)
    assert body["size"] == svc.MAX_PAGE_SIZE


# ── index selection (type filter) ──────────────────────────────────────────────


def test_index_selection_all_types_by_default():
    idx = svc._index_for_types(None)
    for name in ("nes-entities", "ngm-materials", "ngm-courtcases", "jawafdehi-cases"):
        assert name in idx


def test_index_selection_respects_type_filter():
    idx = svc._index_for_types(["entity", "case"])
    assert "nes-entities" in idx
    assert "jawafdehi-cases" in idx
    assert "ngm-materials" not in idx
    assert "ngm-courtcases" not in idx


# ── merge / envelope / facets ──────────────────────────────────────────────────


def _canned_response():
    return {
        "hits": {
            "total": {"value": 3},
            "hits": [
                {
                    "_index": "nes-entities",
                    "_id": "https://jawafdehi.org/entity/person/deuba",
                    "_score": 9.1,
                    "_source": {
                        "iri": "https://jawafdehi.org/entity/person/deuba",
                        "source_app": "nes",
                        "title_ne": "शेर बहादुर देउवा",
                        "title_en": "Sher Bahadur Deuba",
                        "type": "Person",
                        "raw": {},
                    },
                    "highlight": {"title_en": ["<em>Sher</em> Bahadur Deuba"]},
                },
                {
                    "_index": "jawafdehi-cases",
                    "_id": "https://jawafdehi.org/case/budget-scam",
                    "_score": 7.4,
                    "_source": {
                        "iri": "https://jawafdehi.org/case/budget-scam",
                        "source_app": "jawafdehi",
                        "title_en": "Budget scam",
                        "type": "Case",
                        "raw": {"slug": "budget-scam", "case_type": "CORRUPTION"},
                    },
                },
                {
                    "_index": "ngm-courtcases",
                    "_id": "https://jawafdehi.org/courtcase/supreme/081-cr-0081",
                    "_score": 5.0,
                    "_source": {
                        "iri": "https://jawafdehi.org/courtcase/supreme/081-cr-0081",
                        "source_app": "ngm",
                        "title_ne": "081-CR-0081",
                        "type": "jawafdehi:CourtCase",
                        "raw": {"court": "supreme", "case_number": "081-CR-0081"},
                    },
                    # search_after sort values (score, iri) — the last hit's are
                    # encoded into next_cursor by the service.
                    "sort": [5.0, "https://jawafdehi.org/courtcase/supreme/081-cr-0081"],
                },
            ],
        },
        "aggregations": {
            "by_index": {
                "buckets": [
                    {"key": "nes-entities", "doc_count": 1},
                    {"key": "jawafdehi-cases", "doc_count": 1},
                    {"key": "ngm-courtcases", "doc_count": 1},
                ]
            },
            "entity_type": {
                "buckets": [
                    {"key": "Person", "doc_count": 1},
                    {"key": "Case", "doc_count": 1},
                ]
            },
            "case_type": {"buckets": [{"key": "CORRUPTION", "doc_count": 1}]},
            "tags": {"buckets": [{"key": "procurement", "doc_count": 1}]},
        },
    }


def test_search_merges_into_common_envelope():
    client = MagicMock()
    client.search.return_value = _canned_response()
    out = SearchService(client=client).search(q="deuba", page=1, page_size=10)

    assert out["count"] == 3
    assert len(out["results"]) == 3

    # Hits keep OpenSearch (cross-type) order; each carries the common envelope.
    types = [r["type"] for r in out["results"]]
    assert types == ["entity", "case", "courtcase"]

    entity = out["results"][0]
    assert set(entity) >= {
        "type",
        "id",
        "source_app",
        "title",
        "snippet",
        "score",
        "url",
        "api_url",
        "matched_fields",
        "extra",
    }
    assert entity["title"] == {"ne": "शेर बहादुर देउवा", "en": "Sher Bahadur Deuba"}
    assert entity["snippet"]["en"].startswith("<em>Sher</em>")
    assert "title_en" in entity["matched_fields"]
    # Entity URL is a SAME-ORIGIN relative SPA path (IRI tail after /entity/), not
    # the absolute IRI jammed into the path (which would 404 in the router).
    assert entity["url"] == "/entity/person/deuba"

    # Case envelope: url/api_url derive from the slug in raw.
    case = out["results"][1]
    assert case["url"] == "/case/budget-scam"
    assert case["api_url"] == "/api/cases/budget-scam/"
    assert case["extra"]["case_type"] == "CORRUPTION"

    # Court case envelope: frontend url is a same-origin SPA path (IRI tail after
    # /courtcase/); api_url derives from court + case_number.
    courtcase = out["results"][2]
    assert courtcase["url"] == "/courtcase/supreme/081-cr-0081"
    assert courtcase["api_url"] == "/api/courtcases/supreme/081-CR-0081/"


def test_entity_frontend_path_strips_iri_to_tail():
    """Entity URLs become same-origin SPA paths (/entity/<tail>), not the IRI."""
    assert (
        svc._entity_frontend_path(
            "https://jawafdehi.org/entity/organization/education/campus/tu-amrit-campus"
        )
        == "/entity/organization/education/campus/tu-amrit-campus"
    )
    # A bare tail (no scheme) is preserved.
    assert svc._entity_frontend_path("person/deuba") == "/entity/person/deuba"
    assert svc._entity_frontend_path(None) is None


def test_iri_relative_path_leaves_marker_less_absolute_url_unchanged():
    """A foreign/malformed absolute URL (no marker) is returned as-is, never
    re-prefixed into a ``/material/https://...`` link that would 404."""
    foreign = "https://example.org/some/other/path"
    assert svc._iri_relative_path(foreign, svc._MATERIAL_IRI_MARKER) == foreign
    # A bare tail (no scheme, no marker) still gets the marker prefix.
    assert (
        svc._iri_relative_path("ciaa/press-2081", svc._MATERIAL_IRI_MARKER)
        == "/material/ciaa/press-2081"
    )


def test_frontend_url_strips_material_and_courtcase_iris_to_spa_paths():
    """Material + court-case hits link to same-origin SPA paths, not the IRI."""
    material = svc._frontend_url(
        "material", {"iri": "https://jawafdehi.org/material/ciaa/press-2081-042"}
    )
    assert material == "/material/ciaa/press-2081-042"

    # Multi-segment source (e.g. the court-case-derived material) is preserved.
    nested = svc._frontend_url(
        "material", {"iri": "https://jawafdehi.org/material/court/supreme.081-cr-0081"}
    )
    assert nested == "/material/court/supreme.081-cr-0081"

    courtcase = svc._frontend_url(
        "courtcase",
        {"iri": "https://jawafdehi.org/courtcase/supreme/081-cr-0081"},
    )
    assert courtcase == "/courtcase/supreme/081-cr-0081"

    # No IRI -> no URL (graceful, not a crash).
    assert svc._frontend_url("material", {}) is None
    assert svc._frontend_url("courtcase", {}) is None


def test_search_facets_count_per_type():
    client = MagicMock()
    client.search.return_value = _canned_response()
    out = SearchService(client=client).search(q="x")
    assert out["counts"] == {"entity": 1, "case": 1, "courtcase": 1}


def test_search_passes_selected_indices_to_client():
    client = MagicMock()
    client.search.return_value = _canned_response()
    SearchService(client=client).search(q="x", types=["entity"])
    _, kwargs = client.search.call_args
    assert kwargs["index"] == "nes-entities"


# ── 503 on cluster down (hard dependency, no fallback) ──────────────────────────


def test_search_raises_unavailable_when_cluster_down():
    client = MagicMock()
    client.search.side_effect = ConnectionError("no route to cluster")
    with pytest.raises(SearchUnavailable):
        SearchService(client=client).search(q="x")


# ── search_after cursor deep-paging ─────────────────────────────────────────────


def test_build_query_always_sorts_for_stable_paging():
    body = build_query(q="x")
    # Deterministic total order: score desc, iri asc tiebreaker.
    assert body["sort"] == svc.SORT_SPEC
    assert {"_score": {"order": "desc"}} in body["sort"]
    assert {"iri": {"order": "asc"}} in body["sort"]


def test_build_query_offset_mode_uses_from_not_search_after():
    body = build_query(q="x", page=2, page_size=10)
    assert body["from"] == 10
    assert "search_after" not in body


def test_build_query_cursor_mode_uses_search_after_and_omits_from():
    body = build_query(q="x", search_after=[5.0, "iri:z"])
    assert body["search_after"] == [5.0, "iri:z"]
    assert "from" not in body  # search_after + from is invalid


def test_cursor_roundtrip():
    values = [7.5, "https://jawafdehi.org/entity/person/x"]
    assert decode_cursor(encode_cursor(values)) == values


def test_decode_cursor_rejects_garbage():
    with pytest.raises(SearchError):
        decode_cursor("!!!not-base64!!!")
    with pytest.raises(SearchError):
        decode_cursor(encode_cursor({"not": "a list"}))


def test_search_emits_next_cursor_when_page_is_full():
    client = MagicMock()
    client.search.return_value = _canned_response()  # 3 hits, last carries sort
    # page_size == number of hits → page is "full" → a next_cursor is offered.
    out = SearchService(client=client).search(q="x", page_size=3)
    assert out["next_cursor"] is not None
    # The cursor decodes back to the LAST hit's sort values.
    assert decode_cursor(out["next_cursor"]) == [
        5.0,
        "https://jawafdehi.org/courtcase/supreme/081-cr-0081",
    ]


def test_search_no_next_cursor_on_short_page():
    client = MagicMock()
    client.search.return_value = _canned_response()  # 3 hits
    out = SearchService(client=client).search(q="x", page_size=10)  # not full
    assert out["next_cursor"] is None


def test_search_cursor_decoded_and_passed_to_client():
    client = MagicMock()
    client.search.return_value = _canned_response()
    cursor = encode_cursor([9.1, "https://jawafdehi.org/entity/person/deuba"])
    SearchService(client=client).search(q="x", cursor=cursor)
    _, kwargs = client.search.call_args
    assert kwargs["body"]["search_after"] == [
        9.1,
        "https://jawafdehi.org/entity/person/deuba",
    ]
    assert "from" not in kwargs["body"]


def test_search_rejects_overdeep_offset():
    client = MagicMock()
    client.search.return_value = _canned_response()
    # page * page_size beyond the result window → 400 (SearchError), no query run.
    deep_page = (svc.MAX_OFFSET_RESULT_WINDOW // 10) + 5
    with pytest.raises(SearchError):
        SearchService(client=client).search(q="x", page=deep_page, page_size=10)
    client.search.assert_not_called()


# ── sort modes ───────────────────────────────────────────────────────────────


def test_build_query_default_sort_is_relevance():
    body = build_query(q="x")
    assert body["sort"] == svc.SORT_SPEC


def test_build_query_newest_sorts_by_date_desc_then_iri():
    body = build_query(q="x", sort="newest")
    assert body["sort"][0] == {"date": {"order": "desc", "missing": "_last"}}
    # iri tiebreaker preserved so cursor paging stays stable on a non-unique date.
    assert body["sort"][-1] == {"iri": {"order": "asc"}}
    # _score is NOT the primary key (would make date sort meaningless).
    assert body["sort"][0] != {"_score": {"order": "desc"}}


def test_build_query_oldest_sorts_by_date_asc():
    body = build_query(q="x", sort="oldest")
    assert body["sort"][0] == {"date": {"order": "asc", "missing": "_last"}}


def test_build_query_title_sorts_by_keyword_subfield():
    body = build_query(q="x", sort="title")
    assert body["sort"][0]["title_en.keyword"]["order"] == "asc"
    assert body["sort"][-1] == {"iri": {"order": "asc"}}


def test_build_query_featured_sorts_by_weight_then_date_then_iri():
    body = build_query(q="x", sort="featured")
    # ``unmapped_type`` keeps the query valid against an index whose mapping has no
    # ``weight`` at all; without it OpenSearch hard-errors instead of skipping.
    assert body["sort"][0] == {
        "weight": {"order": "desc", "missing": 0, "unmapped_type": "integer"}
    }
    assert body["sort"][1] == svc._sort_spec("newest")[0]
    assert body["sort"][-1] == {"iri": {"order": "asc"}}


def test_featured_is_an_allowed_sort_mode():
    """The API ``sort`` ChoiceField is built from ALL_SORTS, so absence here is a 400."""
    assert "featured" in svc.ALL_SORTS


def test_featured_tiebreakers_are_the_newest_spec_verbatim():
    """Why featured collapses onto newest once weights tie — the equal-weight case
    while nothing is curated. This asserts the clause, not the resulting order."""
    assert svc._sort_spec("featured")[1:] == svc._sort_spec("newest")


def test_serialize_hit_surfaces_weight_so_a_featured_order_explains_itself():
    hit = {
        "_index": "jawafdehi-cases",
        "_source": {"iri": "https://jawafdehi.org/case/x", "weight": 50},
    }
    assert svc._serialize_hit(hit)["extra"]["weight"] == 50


def test_serialize_hit_omits_weight_for_a_doc_indexed_before_the_field():
    hit = {"_index": "jawafdehi-cases", "_source": {"iri": "https://jawafdehi.org/case/x"}}
    assert "weight" not in svc._serialize_hit(hit)["extra"]


# ── facet filters ────────────────────────────────────────────────────────────


def test_build_query_no_filter_clause_by_default():
    body = build_query(q="x")
    assert body["query"]["bool"]["filter"] == []


def test_build_query_entity_type_filter_targets_type_field():
    body = build_query(q="x", filters={"entity_type": ["Person", "Organization"]})
    clauses = body["query"]["bool"]["filter"]
    assert {"terms": {"type": ["Person", "Organization"]}} in clauses


def test_build_query_case_type_and_tags_filters():
    body = build_query(
        q="x", filters={"case_type": ["CORRUPTION"], "tags": ["procurement"]}
    )
    clauses = body["query"]["bool"]["filter"]
    assert {"terms": {"case_type": ["CORRUPTION"]}} in clauses
    # tags filter the shared keywords field.
    assert {"terms": {"keywords": ["procurement"]}} in clauses


def test_build_query_ignores_unknown_filter_and_empty_values():
    body = build_query(q="x", filters={"bogus": ["v"], "tags": []})
    assert body["query"]["bool"]["filter"] == []


def test_build_query_empty_q_is_match_all_browse():
    """Empty/blank q → match_all browse (NOT an empty multi_match matching nothing)."""
    for empty in ("", "   ", None):
        body = build_query(q=empty)
        bq = body["query"]["bool"]
        assert bq["must"] == [{"match_all": {}}], empty
        # No phrase/should clause when there's no term.
        assert "should" not in bq or bq["should"] == []


def test_build_query_empty_q_browse_still_applies_filters_sort_paging():
    body = build_query(
        q="", filters={"entity_type": ["Person"]}, sort="newest", page=2, page_size=5
    )
    bq = body["query"]["bool"]
    assert bq["must"] == [{"match_all": {}}]
    assert {"terms": {"type": ["Person"]}} in bq["filter"]
    assert body["sort"][0] == {"date": {"order": "desc", "missing": "_last"}}
    assert body["from"] == 5  # page 2 × size 5


# ── exposed facet aggregations + envelope ──────────────────────────────────────


def test_build_query_includes_facet_aggregations():
    body = build_query(q="x")
    for agg in ("by_index", "entity_type", "case_type", "tags"):
        assert agg in body["aggs"], agg
    assert body["aggs"]["entity_type"]["terms"]["field"] == "type"
    assert body["aggs"]["tags"]["terms"]["field"] == "keywords"


def test_search_envelope_carries_named_facets():
    client = MagicMock()
    client.search.return_value = _canned_response()
    out = SearchService(client=client).search(q="x")
    # Per-type counts remain separate from the refine facets.
    assert out["counts"] == {"entity": 1, "case": 1, "courtcase": 1}
    facets = out["facets"]
    assert facets["entity_type"] == [
        {"name": "Person", "count": 1},
        {"name": "Case", "count": 1},
    ]
    assert facets["case_type"] == [{"name": "CORRUPTION", "count": 1}]
    assert facets["tags"] == [{"name": "procurement", "count": 1}]


def test_search_threads_sort_and_filters_to_client():
    client = MagicMock()
    client.search.return_value = _canned_response()
    out = SearchService(client=client).search(
        q="x", sort="newest", filters={"case_type": ["CORRUPTION"]}
    )
    assert out["sort"] == "newest"
    _, kwargs = client.search.call_args
    body = kwargs["body"]
    assert body["sort"][0] == {"date": {"order": "desc", "missing": "_last"}}
    assert {"terms": {"case_type": ["CORRUPTION"]}} in body["query"]["bool"]["filter"]


# ── status facet (case lifecycle) + denormalized case card ─────────────────────


def test_build_query_includes_status_facet_and_filter():
    """The ``status`` param is backed by the dedicated ``case_status`` field, NOT
    the generic ``status`` (which holds NGM's scraper enrichment flag)."""
    assert svc.FACET_FIELDS["status"] == "case_status"
    body = build_query(q="x", filters={"status": ["ongoing"]})
    assert body["aggs"]["status"]["terms"]["field"] == "case_status"
    assert {"terms": {"case_status": ["ongoing"]}} in body["query"]["bool"]["filter"]


# ── range filters (बिगो amount) ────────────────────────────────────────────────
#
# The SECOND filter kind. Everything above is exact-match ``terms``; these emit a
# ``range`` clause, and the mechanism is deliberately field-agnostic so
# date_from/date_to can reuse it instead of growing a second one.


def test_range_fields_map_bigo_bounds_to_one_numeric_field():
    """Both params address the SAME indexed field, one bound each."""
    assert svc.RANGE_FIELDS["bigo_min"] == ("bigo", "gte")
    assert svc.RANGE_FIELDS["bigo_max"] == ("bigo", "lte")


def test_build_query_bigo_min_emits_a_range_clause():
    """A lower bound is inclusive (``gte``) — "over रु १ करोड" includes रु १ करोड."""
    body = build_query(q="x", ranges={"bigo_min": 10_000_000})
    assert {"range": {"bigo": {"gte": 10_000_000}}} in body["query"]["bool"]["filter"]


def test_build_query_bigo_max_emits_an_upper_bound():
    """And an upper bound is inclusive too (``lte``), for symmetry."""
    body = build_query(q="x", ranges={"bigo_max": 500_000})
    assert {"range": {"bigo": {"lte": 500_000}}} in body["query"]["bool"]["filter"]


def test_build_query_merges_both_bounds_into_a_single_range_clause():
    """One bounded interval, not two clauses that read as unrelated constraints."""
    body = build_query(q="x", ranges={"bigo_min": 10_000_000, "bigo_max": 10**11})
    clauses = body["query"]["bool"]["filter"]
    assert clauses == [{"range": {"bigo": {"gte": 10_000_000, "lte": 10**11}}}]


def test_build_query_range_clause_targets_the_promoted_field_not_the_card_copy():
    """``raw`` is mapped ``enabled: false``, so a clause on ``raw.card.bigo`` would
    match nothing. The filter must name the promoted top-level field."""
    body = build_query(q="x", ranges={"bigo_min": 1})
    (clause,) = body["query"]["bool"]["filter"]
    assert set(clause["range"]) == {"bigo"}


def test_build_query_no_range_clause_by_default():
    """No bound requested → no clause. An implicit ``bigo >= 0`` would drop every
    non-case result from an ordinary search."""
    assert build_query(q="x")["query"]["bool"]["filter"] == []
    assert build_query(q="x", ranges={})["query"]["bool"]["filter"] == []


def test_build_query_ignores_unknown_range_param_and_none_bounds():
    """Unknown params are ignored (the builder reads RANGE_FIELDS, not the caller's
    keys), and ``None`` means "not requested" — mirrors the ``terms`` behaviour."""
    body = build_query(
        q="x", ranges={"bogus_min": 5, "bigo_min": None, "bigo_max": None}
    )
    assert body["query"]["bool"]["filter"] == []


def test_build_query_keeps_a_zero_lower_bound():
    """``0`` is a real bound. Skipping on falsiness rather than ``is None`` would
    silently drop it and widen the search the user asked to narrow."""
    body = build_query(q="x", ranges={"bigo_min": 0})
    assert {"range": {"bigo": {"gte": 0}}} in body["query"]["bool"]["filter"]


def test_build_query_range_composes_with_terms_filters():
    """Both filter kinds are ANDed into the same bool ``filter`` — the amount
    bound narrows a case-type/status selection rather than replacing it."""
    body = build_query(
        q="x",
        filters={"case_type": ["CORRUPTION"], "status": ["ongoing"]},
        ranges={"bigo_min": 10_000_000},
    )
    clauses = body["query"]["bool"]["filter"]
    assert {"terms": {"case_type": ["CORRUPTION"]}} in clauses
    assert {"terms": {"case_status": ["ongoing"]}} in clauses
    assert {"range": {"bigo": {"gte": 10_000_000}}} in clauses


def test_build_query_range_applies_in_browse_mode():
    """The case list browses with an empty ``q``; the amount bound must still
    narrow it (an empty query is the primary way this filter gets used)."""
    body = build_query(q="", ranges={"bigo_min": 10_000_000}, sort="newest")
    bq = body["query"]["bool"]
    assert bq["must"] == [{"match_all": {}}]
    assert {"range": {"bigo": {"gte": 10_000_000}}} in bq["filter"]


def test_build_query_range_clause_order_is_stable():
    """The DSL is built from RANGE_FIELDS, not the caller's dict, so query-string
    order can't change the emitted body (keeps it diffable + cacheable)."""
    assert build_query(q="x", ranges={"bigo_max": 9, "bigo_min": 1}) == build_query(
        q="x", ranges={"bigo_min": 1, "bigo_max": 9}
    )


def test_search_threads_ranges_to_client():
    """``search()`` forwards ``ranges`` to the builder — the bound reaches the
    cluster, not just the pure query function the other tests exercise."""
    client = MagicMock()
    client.search.return_value = _canned_response()
    SearchService(client=client).search(q="x", ranges={"bigo_min": 10_000_000})
    body = client.search.call_args.kwargs["body"]
    assert {"range": {"bigo": {"gte": 10_000_000}}} in body["query"]["bool"]["filter"]


def _case_card_response():
    """A single case hit carrying the denormalized ``raw.card`` render payload."""
    return {
        "hits": {
            "total": {"value": 1},
            "hits": [
                {
                    "_index": "jawafdehi-cases",
                    "_id": "https://jawafdehi.org/case/land-grab",
                    "_score": 4.2,
                    "_source": {
                        "iri": "https://jawafdehi.org/case/land-grab",
                        "source_app": "jawafdehi",
                        "title_en": "Land grab",
                        "type": "Case",
                        "case_status": "ongoing",
                        "raw": {
                            "slug": "land-grab",
                            "case_type": "CORRUPTION",
                            "card": {
                                "slug": "land-grab",
                                "status": "ongoing",
                                "tags": ["land"],
                                "timeline": [{"date": "2024-01-01", "title": "Filed"}],
                                "entities": [],
                            },
                        },
                    },
                }
            ],
        },
        "aggregations": {},
    }


def test_case_result_surfaces_denormalized_card():
    client = MagicMock()
    client.search.return_value = _case_card_response()
    out = SearchService(client=client).search(q="land", types=["case"])
    result = out["results"][0]
    assert result["type"] == "case"
    # The whole card payload (incl. timeline/major events) rides on the result,
    # so the SPA renders without a follow-up /api/cases/{slug}/ fetch.
    assert result["card"]["slug"] == "land-grab"
    assert result["card"]["status"] == "ongoing"
    assert result["card"]["timeline"] == [{"date": "2024-01-01", "title": "Filed"}]


def test_non_case_result_has_no_card_key():
    """Only case hits carry a ``card``; other types keep the lean envelope."""
    client = MagicMock()
    client.search.return_value = _canned_response()
    out = SearchService(client=client).search(q="x")
    entity = out["results"][0]
    assert entity["type"] == "entity"
    assert "card" not in entity
