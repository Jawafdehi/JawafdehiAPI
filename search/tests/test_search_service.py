"""SearchService tests — bilingual query DSL, envelope/merge/facets, 503.

No live cluster: a MagicMock client returns a canned OpenSearch response. Tests
assert the query DSL hits the bilingual fields (incl. ``title_translit``), that
the multi-index hits merge into the common envelope with per-type facet counts,
and that a transport error becomes ``SearchUnavailable`` (→ HTTP 503).
"""

from __future__ import annotations

import json
import string
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
    """The required (recall) multi_match clause from the tuned bool query.

    Shape-tolerant on purpose. A query carrying a fuzzy-ELIGIBLE token wraps two
    recall routes in a nested bool, so ``must[0]`` is that bool and the exact
    clause is its first ``should``; anything else (Devanagari, an identifier, a
    browse) keeps ``must[0]`` as the clause itself. Every caller here wants the
    exact route either way, so resolve it rather than making each test know which
    shape its query produced.
    """
    must = body["query"]["bool"]["must"][0]
    if "multi_match" in must:
        return must["multi_match"]
    return must["bool"]["should"][0]["multi_match"]


def _fuzzy_multi_match(body):
    """The damped fuzzy recall clause — the SECOND ``should`` of the nested bool."""
    return body["query"]["bool"]["must"][0]["bool"]["should"][1]["multi_match"]


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


def test_serialize_hit_surfaces_the_court_geography_a_client_filtered_on():
    """A court-case hit carries back the three court fields the new ?court_type /
    ?district / ?province filters select on, so a client can render and re-filter
    without a second lookup. Names are load-bearing: they are the response half of
    the request contract, and a typo here loses a key silently and forever."""
    hit = {
        "_index": "ngm-courtcases",
        "_source": {
            "iri": "https://jawafdehi.org/courtcase/kathmandudc/081-CR-0081",
            "court_type": "district",
            "court_district": "Kathmandu",
            "court_province": "Bagmati",
            "raw": {"court": "kathmandudc", "case_number": "081-CR-0081"},
        },
    }
    extra = svc._serialize_hit(hit)["extra"]
    assert extra["court_type"] == "district"
    assert extra["court_district"] == "Kathmandu"
    assert extra["court_province"] == "Bagmati"
    # ``court`` stays sourced from ``raw``, which every court-case doc has ever
    # carried — so extra.court keeps working on pre-rebuild docs.
    assert extra["court"] == "kathmandudc"


def test_serialize_hit_omits_court_geography_before_the_rebuild():
    """The four fields are inert until ``reindex_courtcases --rebuild``, so a doc
    from the current generation has none of them. They must be ABSENT, not None —
    a client tells "no district" from "high court, districts do not apply" by the
    key's presence."""
    hit = {
        "_index": "ngm-courtcases",
        "_source": {
            "iri": "https://jawafdehi.org/courtcase/kathmandudc/081-CR-0081",
            "raw": {"court": "kathmandudc"},
        },
    }
    extra = svc._serialize_hit(hit)["extra"]
    for key in ("court_type", "court_district", "court_province"):
        assert key not in extra, key


def test_serialize_hit_omits_district_for_a_high_court_but_keeps_province():
    """The shape that makes ?district= mean "a district court's own district":
    a high court indexes a province and no district at all."""
    hit = {
        "_index": "ngm-courtcases",
        "_source": {
            "iri": "https://jawafdehi.org/courtcase/patanhc/081-CR-0001",
            "court_type": "high",
            "court_province": "Bagmati",
            "raw": {"court": "patanhc"},
        },
    }
    extra = svc._serialize_hit(hit)["extra"]
    assert extra["court_province"] == "Bagmati"
    assert "court_district" not in extra


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


def test_build_query_includes_court_type_facet_and_filter():
    """``court_type`` is the promoted court tier (district/high/supreme/special)
    on NGM court-case docs — named after the DB column it comes from."""
    assert svc.FACET_FIELDS["court_type"] == "court_type"
    body = build_query(q="x", filters={"court_type": ["supreme", "special"]})
    assert body["aggs"]["court_type"]["terms"]["field"] == "court_type"
    assert {"terms": {"court_type": ["supreme", "special"]}} in body["query"]["bool"][
        "filter"
    ]


def test_build_query_court_filter_selects_an_arbitrary_set_of_courts():
    """The point of the one-court facet: a mixed set ACROSS tiers, which the
    court_type/district pair cannot express (those AND, so two tiers x two
    districts returns the cross-product)."""
    assert svc.FACET_FIELDS["court"] == "court"
    body = build_query(q="x", filters={"court": ["kathmandudc", "patanhc", "supreme"]})
    assert body["aggs"]["court"]["terms"]["field"] == "court"
    assert {"terms": {"court": ["kathmandudc", "patanhc", "supreme"]}} in body["query"][
        "bool"
    ]["filter"]


def test_build_query_district_and_province_filters_target_court_fields():
    """The reader-facing params map to the promoted ``court_*`` keywords."""
    assert svc.FACET_FIELDS["district"] == "court_district"
    assert svc.FACET_FIELDS["province"] == "court_province"
    body = build_query(
        q="x", filters={"district": ["Kathmandu"], "province": ["Bagmati"]}
    )
    clauses = body["query"]["bool"]["filter"]
    assert {"terms": {"court_district": ["Kathmandu"]}} in clauses
    assert {"terms": {"court_province": ["Bagmati"]}} in clauses


def test_district_facet_agg_holds_every_district_at_once():
    """All 77 districts: at the 50 default, the least-frequent districts would be
    silently pushed out of the facet."""
    body = build_query(q="x")
    assert body["aggs"]["district"]["terms"]["size"] >= 77


def test_court_facet_agg_holds_every_court_at_once():
    """All 97 courts carry cases, so at the 50 default a third of them would be
    missing from the facet a court picker is built from, counts silently zeroed."""
    from courts.geography import ALL_COURT_IDENTIFIERS

    body = build_query(q="x")
    assert body["aggs"]["court"]["terms"]["size"] >= len(ALL_COURT_IDENTIFIERS)


# ── facet-value search (facet_q) ────────────────────────────────────────────────


def test_facet_include_regex_case_folds_and_escapes():
    """Cased letters become ``[xX]`` classes (Lucene RegExp has no ``(?i)``);
    regex operators in user text are escaped to literals."""
    assert svc._facet_include_regex("ab") == ".*[aA][bB].*"
    assert svc._facet_include_regex("c++") == ".*[cC]\\+\\+.*"
    assert svc._facet_include_regex("a|b.c") == ".*[aA]\\|[bB]\\.[cC].*"
    # Non-operator characters — Devanagari letters, combining vowel signs,
    # digits, spaces — pass through verbatim.
    assert svc._facet_include_regex("घुस 1") == ".*घुस 1.*"


#: Lucene's RegExp reserved characters, transcribed from the ``RegExp`` javadoc
#: syntax table — the core operators plus every optional-syntax one (``#``, ``@``,
#: ``&``, ``<``, ``>``, ``~``), which OpenSearch enables by constructing
#: ``RegExp`` with ``ALL`` flags.
#:
#: Deliberately a LITERAL here and NOT read from ``svc._LUCENE_REGEXP_SPECIAL``:
#: a test that iterates the production set cannot detect that set shrinking, it
#: just iterates fewer members and stays green. This is the independent copy that
#: makes the assertions below bite.
LUCENE_REGEXP_OPERATORS = frozenset('.?+*|{}[]()"\\#@&<>~')


def test_lucene_operator_set_is_complete():
    """The escape set must BE Lucene's operator set — pinned against the literal
    above, so it can neither shrink (an operator reaching Lucene live) nor grow
    (a literal over-escaped, making real bucket keys unmatchable)."""
    assert svc._LUCENE_REGEXP_SPECIAL == LUCENE_REGEXP_OPERATORS


def test_facet_include_regex_escapes_every_lucene_operator():
    """Every operator, one at a time, driven off the independent literal.

    The set shrinks silently otherwise: drop ``@`` (ANYSTRING) and
    ``?facet_q=tags:a@b`` becomes a wildcard returning every ``a…b`` bucket
    instead of the literal; drop ``[`` and the same param emits the unterminated
    ``.*[.*``, a PatternSyntaxException the service can only report as a 503.
    Neither surfaces in a hit assertion.

    This also pins the BRANCH ORDER in ``_facet_include_regex``: the cased-letter
    arm runs before the escape arm, so an operator that were ever also cased
    would silently skip escaping.
    """
    for ch in LUCENE_REGEXP_OPERATORS:
        assert svc._facet_include_regex(ch) == f".*\\{ch}.*", ch


def test_facet_include_regex_leaves_every_other_character_literal():
    """The inverse sweep: every printable NON-operator must stay literal (cased
    ASCII folding to a ``[xX]`` class), so the escape set cannot grow."""
    for ch in string.printable:
        if ch in LUCENE_REGEXP_OPERATORS:
            continue
        folded = f"[{ch.lower()}{ch.upper()}]" if ch.lower() != ch.upper() else ch
        assert svc._facet_include_regex(ch) == f".*{folded}.*", repr(ch)


def test_facet_include_regex_cannot_smuggle_an_operator():
    """The docstring's actual security claim: user text can never put a live
    operator into the aggregation. Feed it EVERY operator at once and require the
    emitted middle to be exactly those characters, each backslash-escaped — no
    stray class, no bare operator, nothing that could widen the match."""
    hostile = "".join(sorted(LUCENE_REGEXP_OPERATORS))
    pattern = svc._facet_include_regex(hostile)
    assert pattern.startswith(".*") and pattern.endswith(".*")
    assert pattern[2:-2] == "".join("\\" + ch for ch in hostile)


def test_build_query_facet_q_narrows_only_the_named_facets_buckets():
    """The include regex lands on the named agg alone — the query, filters, and
    every other facet's agg are byte-identical to a facet_q-less build."""
    plain = build_query(q="x")
    body = build_query(q="x", facet_queries={"tags": "घुस"})
    assert body["aggs"]["tags"]["terms"]["include"] == ".*घुस.*"
    assert body["query"] == plain["query"]
    for param in svc.FACET_FIELDS:
        if param == "tags":
            continue
        assert body["aggs"][param] == plain["aggs"][param], param
    # Still ordered by count (the terms-agg default): no order override emitted.
    assert "order" not in body["aggs"]["tags"]["terms"]
    # And the size cap is unchanged — include filters BEFORE the size cut, so
    # the match runs over the full term set, not the default top-N slice.
    assert body["aggs"]["tags"]["terms"]["size"] == svc.DEFAULT_FACET_AGG_SIZE


def test_build_query_facet_q_is_repeatable_across_facets():
    body = build_query(
        q="x", facet_queries={"tags": "कर", "case_type": "corr"}
    )
    assert body["aggs"]["tags"]["terms"]["include"] == ".*कर.*"
    assert (
        body["aggs"]["case_type"]["terms"]["include"]
        == ".*[cC][oO][rR][rR].*"
    )


def test_every_facet_field_has_an_aggregation():
    """The aggs are GENERATED from FACET_FIELDS, so a registry entry can never
    exist without its aggregation — this pins that refactor (the aggs used to be
    hand-listed, and a missing one served an empty facet list forever, silently)."""
    body = build_query(q="x")
    for param, field in svc.FACET_FIELDS.items():
        assert body["aggs"][param]["terms"]["field"] == field, param
        assert body["aggs"][param]["terms"]["size"] >= 1


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


# ── range filters (date) ────────────────────────────────────────────────────────
#
# The bounds the field-agnostic range mechanism was pre-designed for: two more
# RANGE_FIELDS entries over the shared Gregorian ``date`` field, nothing else.


def test_range_fields_map_date_bounds_to_the_shared_date_field():
    """Both params address the SAME indexed ``date`` field, one bound each."""
    assert svc.RANGE_FIELDS["date_from"] == ("date", "gte")
    assert svc.RANGE_FIELDS["date_to"] == ("date", "lte")


def test_build_query_date_from_emits_a_gte_range_clause():
    """A lower bound is inclusive — "from 2020" includes 2020-01-01 itself."""
    body = build_query(q="x", ranges={"date_from": "2020-01-15"})
    assert {"range": {"date": {"gte": "2020-01-15"}}} in body["query"]["bool"]["filter"]


def test_build_query_merges_date_bounds_into_a_single_range_clause():
    """One bounded interval on ``date``, exactly like the बिगो pair."""
    body = build_query(
        q="x", ranges={"date_from": "2020-01-01", "date_to": "2021-12-31"}
    )
    clauses = body["query"]["bool"]["filter"]
    assert clauses == [
        {"range": {"date": {"gte": "2020-01-01", "lte": "2021-12-31"}}}
    ]


def test_build_query_date_and_bigo_ranges_are_separate_clauses():
    """Bounds on DIFFERENT fields must not merge — one ``range`` clause per field."""
    body = build_query(
        q="x", ranges={"bigo_min": 500, "date_from": "2020-01-01"}
    )
    clauses = body["query"]["bool"]["filter"]
    assert {"range": {"bigo": {"gte": 500}}} in clauses
    assert {"range": {"date": {"gte": "2020-01-01"}}} in clauses
    assert len(clauses) == 2


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


# ── बिगो extent (the histogram's axis and bars) ────────────────────────────────
#
# A THIRD aggregation kind, alongside the per-type counts and the terms facets:
# the corpus extent of a range field. What makes it different is that it must not
# narrow with the results — the bars ARE the control the reader clicks.


def test_build_query_requests_the_bigo_extent_as_a_global_agg():
    """The extent must escape the query context.

    Every other agg here is a refine facet, which correctly narrows with the
    result set. The extent must not: if it tracked the active range, selecting a
    bar would delete the bars on either side of it and the reader could never
    widen back out. ``global`` is what buys that.
    """
    extent = build_query(q="x", types=["case"])["aggs"]["bigo_extent"]
    assert extent["global"] == {}
    assert extent["aggs"]["stats"] == {"stats": {"field": "bigo"}}


def test_build_query_extent_survives_an_active_bigo_range():
    """Belt and braces on the point above — the agg is byte-identical with a
    bound applied, so the axis never moves under the reader's own selection."""
    unbounded = build_query(q="x", types=["case"])["aggs"]["bigo_extent"]
    bounded = build_query(q="x", types=["case"], ranges={"bigo_min": 10**9})["aggs"][
        "bigo_extent"
    ]
    assert unbounded == bounded


def test_build_query_omits_the_extent_unless_the_search_is_case_only():
    """A ``global`` agg is a second collection pass over every searched index.

    It is not scoped to the case index — it escapes the query context entirely,
    so on an unscoped search it walks entities + materials + court cases too
    (~560k docs in production) and re-runs the ``multi_match`` for each of them
    inside ``distribution``. The cost is decoupled from how selective the query
    is: ``?q=<matches nothing>`` goes from nearly free to a full-corpus scan.

    And nothing reads it there. The SPA gates the control on
    ``selectedType === "case"``, so ``extents`` is discarded on every other
    view. Emitting it only for a case-only search shrinks the global bucket to
    the case index, which is the whole reason the agg is affordable at all.
    """
    assert "bigo_extent" in build_query(q="x", types=["case"])["aggs"]
    # Unscoped: the widest possible bucket, and the SPA cannot render it.
    assert "bigo_extent" not in build_query(q="x")["aggs"]
    assert "bigo_extent" not in build_query(q="x", types=[])["aggs"]
    # Mixed scope still drags the other indices into the global bucket.
    assert "bigo_extent" not in build_query(q="x", types=["case", "entity"])["aggs"]


def test_build_query_omits_the_extent_when_no_case_index_is_in_scope():
    """Only cases carry an amount, so an entity-only search must not pay for a
    corpus-wide aggregation it cannot use."""
    assert "bigo_extent" not in build_query(q="x", types=["entity"])["aggs"]
    assert "bigo_extent" not in build_query(q="x", types=["material"])["aggs"]


def test_search_returns_the_bigo_extent_as_whole_rupees():
    """``stats`` hands back doubles; बिगो is a ``long`` of whole rupees.

    Passing the float through would lose precision past 2**53 — which this corpus
    already reaches, with amounts into the tens of अरब.
    """
    client = MagicMock()
    response = _canned_response()
    response["aggregations"] = {
        "bigo_extent": {
            "stats": {"count": 68, "min": 45220.0, "max": 6.6e10},
            "distribution": {"buckets": {"buckets": []}},
        }
    }
    client.search.return_value = response
    bigo = SearchService(client=client).search(q="x", types=["case"])["extents"]["bigo"]
    # The axis only — the bars have their own test below.
    assert (bigo["min"], bigo["max"], bigo["count"]) == (45220, 66_000_000_000, 68)
    assert isinstance(bigo["max"], int)


def test_search_reports_no_extent_when_nothing_records_an_amount():
    """``count: 0`` with null bounds means "no control to render", NOT a
    zero-width range — the latter would draw a chart with nothing clickable."""
    client = MagicMock()
    response = _canned_response()
    response["aggregations"] = {
        "bigo_extent": {
            "stats": {"count": 0, "min": None, "max": None},
            "distribution": {"buckets": {"buckets": []}},
        }
    }
    client.search.return_value = response
    assert SearchService(client=client).search(q="x", types=["case"])["extents"] == {}


def test_search_reports_no_extent_when_the_agg_was_not_requested():
    """An entity-only search carries no extent block at all."""
    client = MagicMock()
    client.search.return_value = _canned_response()
    assert SearchService(client=client).search(q="x", types=["entity"])["extents"] == {}
# ── alias generations: a hit reports the CONCRETE index, never the alias ─────
#
# Regression (#453): the public index names became ALIASES over numbered
# generations, but a hit's ``_index`` and an ``_index`` agg bucket both report
# the backing index (``jawafdehi-cases-000001``). The type lookup was keyed by
# the alias only, so every result fell through to ``source_app`` — search
# results came back typed "jawafdehi"/"ngm" instead of case/material, the FE
# could no longer build a /case/<slug> link from them, and every per-type facet
# count went to zero.


def test_type_for_index_resolves_a_plain_alias():
    assert svc.type_for_index("jawafdehi-cases") == "case"


def test_type_for_index_resolves_a_generation_backing_index():
    assert svc.type_for_index("jawafdehi-cases-000001") == "case"
    assert svc.type_for_index("ngm-courtcases-000042") == "courtcase"
    assert svc.type_for_index("nes-entities-000007") == "entity"
    assert svc.type_for_index("ngm-materials-000123") == "material"


def test_extent_is_stats_only_and_carries_no_histogram():
    """One agg, three numbers.

    A ``range`` sub-agg for a distribution histogram used to hang off this
    ``global`` bucket, plus a second ``stops`` ladder for slider thumbs. Both are
    gone: the SPA draws a slider over a log ladder it derives from ``min``/``max``,
    so there is nothing for a client to line up against and nothing to keep in
    step across two repos.

    It was also the expensive half — a 14-bucket range agg that re-ran the user's
    ``multi_match`` across the whole global bucket. Anything re-adding it is
    re-adding both that cost and that coupling.
    """
    extent = build_query(q="x", types=["case"])["aggs"]["bigo_extent"]
    assert extent["global"] == {}
    assert extent["aggs"] == {"stats": {"stats": {"field": "bigo"}}}


def test_search_returns_the_extent_as_three_whole_rupee_numbers():
    client = MagicMock()
    response = _canned_response()
    # ``stats`` returns JSON doubles; the corpus already reaches tens of अरब, so a
    # float would lose precision past 2**53.
    response["aggregations"] = {
        "bigo_extent": {"stats": {"count": 68, "min": 45220.0, "max": 6.6e10}}
    }
    client.search.return_value = response
    bigo = SearchService(client=client).search(q="x", types=["case"])["extents"]["bigo"]
    assert bigo == {"min": 45220, "max": 66_000_000_000, "count": 68}
    assert all(isinstance(v, int) for v in bigo.values())


# ── bounded fuzzy matching (design §10) ────────────────────────────────────────
#
# Romanized Nepali has no fixed spelling, so ``coruption``/``baluwatar`` matched
# NOTHING and dead-ended on the empty state. The fix is deliberately narrow: a
# damped second recall route, only for tokens that could plausibly be a
# misspelling, and invisible on every other query.


def test_fuzzy_eligibility_keeps_roman_words_of_four_characters_or_more():
    """Design §10's eligible shape — and the only one a genuine romanization slip
    takes, since Devanagari has no safe edit distance and an identifier's edits
    change WHICH record is meant."""
    assert svc.fuzzy_eligible_tokens("coruption") == ["coruption"]
    # Normalized the same way the analytics stream aggregates (lower + trim).
    assert svc.fuzzy_eligible_tokens("  Baluwatar ") == ["baluwatar"]


def test_fuzzy_eligibility_excludes_everything_design_10_excludes():
    """One ASCII-letters test delivers four of the five exclusions: Devanagari
    fails ``isascii``, and identifiers/case numbers/numerics fail ``isalpha`` on
    their digits and separators. The fifth is the length floor."""
    assert svc.fuzzy_eligible_tokens("देउवा") == []  # not Roman script
    assert svc.fuzzy_eligible_tokens("082-CR-0154") == []  # a case number
    assert svc.fuzzy_eligible_tokens("ciaa/press-2081") == []  # an identifier
    assert svc.fuzzy_eligible_tokens("2024") == []  # entirely numeric
    assert svc.fuzzy_eligible_tokens("job") == []  # under the length floor
    assert svc.fuzzy_eligible_tokens("") == []
    assert svc.fuzzy_eligible_tokens(None) == []


def test_fuzzy_eligibility_keeps_only_the_eligible_tokens_of_a_mixed_query():
    """Ineligible tokens are not dropped from the SEARCH — the exact recall clause
    still matches them. They just never get fuzzed."""
    assert svc.fuzzy_eligible_tokens("बालुवाटार coruption 2081 in") == ["coruption"]


def test_fuzzy_eligibility_honours_the_denylist(monkeypatch):
    """The mechanism ships with an EMPTY denylist, to be populated later from the
    zero-result analytics rather than guessed at — so pin that it is consulted."""
    assert svc.FUZZY_DENYLIST == frozenset()
    monkeypatch.setattr(svc, "FUZZY_DENYLIST", frozenset({"case"}))
    assert svc.fuzzy_eligible_tokens("case files") == ["files"]


def test_build_query_adds_a_second_damped_recall_route_for_an_eligible_query():
    body = build_query(q="coruption")
    must = body["query"]["bool"]["must"]
    assert len(must) == 1
    nested = must[0]["bool"]
    # Satisfied by EITHER route. This is a nested bool inside ``must``, NOT a
    # top-level ``should``: a pure misspelling matches neither the exact recall
    # clause nor the phrase clause, and a top-level should cannot rescue an
    # unsatisfied must — the query would still return nothing, which is the bug.
    assert nested["minimum_should_match"] == 1
    exact, fuzzy = nested["should"]
    # The exact route rides through untouched — same fields, no fuzziness on it.
    assert exact["multi_match"]["fields"] == svc._weighted_query_fields("both")
    assert "fuzziness" not in exact["multi_match"]
    # ...and the fuzzy one is bounded and damped.
    mm = fuzzy["multi_match"]
    assert mm["query"] == "coruption"
    assert mm["fuzziness"] == "AUTO:4,8"
    assert mm["prefix_length"] == 1
    assert mm["boost"] == svc.FUZZY_BOOST
    # ``most_fields``, never ``cross_fields`` — the latter silently DROPS
    # fuzziness (docs/shared/research/opensearch-bilingual-nepali.md §5).
    assert mm["type"] == "most_fields"


def test_both_recall_routes_are_named_so_hits_report_which_one_matched():
    """The did-you-mean weak-match gate READS this rather than inferring it: with
    both routes named, OpenSearch tags every hit with the route(s) that matched, so
    ``_result_set_is_wholly_fuzzy`` can ask whether the page has a genuine anchor.
    """
    nested = build_query(q="coruption")["query"]["bool"]["must"][0]["bool"]
    exact, fuzzy = nested["should"]
    assert exact["multi_match"]["_name"] == svc.EXACT_RECALL_CLAUSE_NAME
    assert fuzzy["multi_match"]["_name"] == svc.FUZZY_RECALL_CLAUSE_NAME
    # Distinct, or the gate could not tell the two apart.
    assert svc.EXACT_RECALL_CLAUSE_NAME != svc.FUZZY_RECALL_CLAUSE_NAME


def test_naming_is_confined_to_the_fuzzy_branch():
    """The no-op guarantee covers the names too: an ineligible query emits no
    ``_name`` anywhere, so its DSL stays byte-identical to the pre-fuzzy one.
    ``test_build_query_is_unchanged_when_no_token_is_eligible`` pins the whole
    clause; this states the reason separately so a future edit cannot quietly
    reintroduce a name on the plain path."""
    for ineligible in ("देउवा", "082-CR-0154", "2024", "job", ""):
        assert "_name" not in json.dumps(build_query(q=ineligible)), ineligible


def test_fuzzy_eligibility_is_capped_so_query_size_cannot_drive_cost():
    """``q`` is an unbounded CharField, so query size is caller-controlled. Matching
    every token EXACTLY was affordable; fuzzing every token is not — each term walks
    the dictionary for two-edit neighbours across four fields, and a large enough
    disjunction is rejected outright by ``max_clause_count``, which this service
    turns into a 503. The cap truncates, so exact recall still covers the whole
    query."""
    # Alphabetic on purpose: a token carrying digits is ineligible anyway, so it
    # would test the ASCII-letters rule rather than the cap.
    long_query = " ".join("word" + string.ascii_lowercase[n % 26] for n in range(200))
    tokens = svc.fuzzy_eligible_tokens(long_query)
    assert len(tokens) == svc.FUZZY_MAX_TOKENS
    # Truncated from the FRONT — the leading terms are the ones a reader meant.
    assert tokens[0] == "worda"
    # Both consumers read the one capped list, so they cannot disagree.
    body = build_query(q=long_query)
    fuzzy = _fuzzy_multi_match(body)
    assert len(fuzzy["query"].split()) == svc.FUZZY_MAX_TOKENS
    assert len(body["suggest"]["text"].split()) == svc.FUZZY_MAX_TOKENS


def test_fuzzy_clause_bounds_its_term_expansion():
    """"Bounded" is this route's whole promise, and a default is not a decision."""
    mm = _fuzzy_multi_match(build_query(q="coruption"))
    assert mm["max_expansions"] == svc.FUZZY_MAX_EXPANSIONS


def test_fuzzy_route_never_queries_the_devanagari_title():
    """Fuzziness is edit distance over analyzed terms, and a Roman token is never
    within two edits of a Devanagari one — the field would cost term expansions and
    match nothing. (Devanagari fuzziness is out of scope per design §10; those
    queries keep normalization and the translit bridge.)"""
    fields = _fuzzy_multi_match(build_query(q="coruption"))["fields"]
    assert not any(f.startswith("title_ne") for f in fields), fields
    assert any(f.startswith("title_en") for f in fields)
    assert any(f.startswith("title_translit") for f in fields)
    assert any(f.startswith("keywords.text") for f in fields)
    assert any(f.startswith("body") for f in fields)


def test_fuzzy_route_queries_only_the_eligible_tokens():
    """Handing the raw ``q`` back to the fuzzy clause would re-admit the very terms
    eligibility just excluded, since ``fuzziness`` applies per term."""
    body = build_query(q="बालुवाटार coruption 2081")
    assert _fuzzy_multi_match(body)["query"] == "coruption"
    # The exact route still carries the WHOLE query, Devanagari and year included.
    assert _recall_multi_match(body)["query"] == "बालुवाटार coruption 2081"


def test_fuzzy_boost_stays_below_every_exact_weight():
    """Design §10: a fuzzy match must never outrank an exact identifier, title,
    name, alias, phrase or correctly-spelled ordinary match. BM25 cannot make that
    a HARD guarantee — ``FUZZY_BOOST`` is the knob, so pin it."""
    exact_weights = [float(f.split("^")[1]) for f in svc._weighted_query_fields("both")]
    assert svc.FUZZY_BOOST < min(exact_weights)
    assert svc.FUZZY_BOOST < svc.PHRASE_BOOST
    # And the fuzzy route reuses the exact route's weights rather than inventing a
    # second scheme that could silently drift out of step with it.
    assert svc.FUZZY_FIELDS == [
        f"title_en^{svc._TITLE_EN_BOOST:g}",
        f"title_translit^{svc._TITLE_TRANSLIT_BOOST:g}",
        f"keywords.text^{svc._KEYWORDS_BOOST:g}",
        f"body^{svc._BODY_BOOST:g}",
    ]


def test_fuzziness_is_capped_at_two_edits():
    """``AUTO:4,8`` — under 4 chars exact, 4–7 one edit, 8+ two. Two is design
    §10's ceiling. Raising it is NOT how the remaining audit queries
    (``melamchee``, ``bhrastachaar``, ``kathmandu`` — 3–4 edits from their indexed
    romanizations) get fixed; that is the romanization card's job. Past two edits
    ``duba``/``deuba``-class collisions arrive faster than real corrections."""
    assert svc.FUZZINESS == "AUTO:4,8"
    assert svc.SUGGEST_MAX_EDITS == 2


# ── the no-op guarantee ───────────────────────────────────────────────────────
#
# The mechanism must be INVISIBLE on every query it cannot help. These two pin the
# emitted DSL as a whole, not just the absence of a fuzziness key.


def test_build_query_is_unchanged_when_no_token_is_eligible():
    for ineligible in ("देउवा", "082-CR-0154", "2024", "job"):
        body = build_query(q=ineligible)
        # The whole clause, not merely the absence of a ``fuzziness`` key.
        assert body["query"]["bool"]["must"] == [
            {
                "multi_match": {
                    "query": ineligible,
                    "fields": svc._weighted_query_fields("both"),
                    "type": "most_fields",
                    "operator": "or",
                }
            }
        ], ineligible
        assert "suggest" not in body, ineligible


def test_browse_carries_neither_a_fuzzy_route_nor_a_suggester():
    """An empty ``q`` has nothing to misspell, and a ``match_all`` browse is the
    primary way the case list is paged."""
    for empty in ("", "   ", None):
        body = build_query(q=empty)
        assert body["query"]["bool"]["must"] == [{"match_all": {}}], empty
        assert "suggest" not in body, empty


# ── did-you-mean (design §11) ──────────────────────────────────────────────────


def test_build_query_requests_a_term_suggester_for_an_eligible_query():
    """On the SAME request — no second round trip — and ``suggest_mode: missing``
    makes it near-free for a query that is spelled correctly."""
    suggest = build_query(q="coruption")["suggest"]
    assert suggest["text"] == "coruption"
    # Two entries, each keyed by the field it suggests from.
    assert set(suggest) == {"text", "title_translit", "keywords.text"}
    for field in ("title_translit", "keywords.text"):
        assert suggest[field]["term"] == {
            "field": field,
            "suggest_mode": "missing",
            "max_edits": 2,
            "prefix_length": 1,
            "min_word_length": 4,
            "size": 1,
        }, field


def test_suggester_avoids_the_stemmed_title_and_the_ocr_body():
    """``title_en`` is Porter-stemmed, so its term dictionary holds ``corrupt`` —
    it would suggest THAT for ``coruption``. ``body`` is OCR text, which is exactly
    the vocabulary a suggestion must not be drawn from. What is left is the curated
    tags (design §11's "approved aliases") plus the unstemmed title romanizations,
    in that order — see the authority test below."""
    assert svc.SUGGEST_FIELDS == ("keywords.text", "title_translit")


def test_suggester_text_is_the_eligible_tokens_only():
    assert build_query(q="बालुवाटार coruption 2081")["suggest"]["text"] == "coruption"


def _zero_hit_response(suggest=None):
    """A real query that matched nothing — the one state did-you-mean is offered in."""
    response: dict = {"hits": {"total": {"value": 0}, "hits": []}, "aggregations": {}}
    if suggest is not None:
        response["suggest"] = suggest
    return response


def _term_suggestion(token, *, options):
    """One ``term``-suggester entry, shaped as OpenSearch returns it."""
    return [{"text": token, "offset": 0, "length": len(token), "options": options}]


def _matched(response, *names):
    """Tag every hit with the recall route(s) that matched it.

    This is what OpenSearch returns for NAMED query clauses, and ``build_query``
    names both routes whenever the fuzzy one is active — so any real
    fuzzy-eligible search comes back carrying these. The weak-match gate reads
    them instead of inferring an anchor from the suggester.
    """
    for hit in response["hits"]["hits"]:
        hit["matched_queries"] = list(names)
    return response


def test_did_you_mean_substitutes_the_suggested_token():
    client = MagicMock()
    client.search.return_value = _zero_hit_response(
        {
            "title_translit": _term_suggestion(
                "coruption", options=[{"text": "corruption", "score": 0.9, "freq": 12}]
            ),
            "keywords.text": _term_suggestion("coruption", options=[]),
        }
    )
    out = SearchService(client=client).search(q="coruption")
    assert out["count"] == 0
    assert out["did_you_mean"] == "corruption"
    # The original (empty) result set is preserved — the suggestion never replaces
    # the query, it only offers to.
    assert out["results"] == []
    assert out["query"] == "coruption"


def test_did_you_mean_prefers_the_curated_field_over_a_higher_SCORING_romanization():
    """The regression that only live data exposed.

    Ranking candidates on score alone picks junk. Measured against the production
    corpus, ``melamchee`` draws ``melamchi`` (0.75) from the curated
    ``keywords.text`` and ``maramchee`` (0.78) from the machine-romanized
    ``title_translit`` — and EVERY candidate comes back ``freq: 1``, so a
    noisy-channel ``score x log(freq)`` prior still picks the wrong one.
    ``title_translit`` holds one machine transliteration per title, so its
    near-neighbours are mostly noise; design §11's "approved aliases" is the
    tiebreak, and :data:`SUGGEST_FIELDS` order encodes it.
    """
    suggest = {
        "title_translit": _term_suggestion(
            "melamchee", options=[{"text": "maramchee", "score": 0.78, "freq": 1}]
        ),
        "keywords.text": _term_suggestion(
            "melamchee", options=[{"text": "melamchi", "score": 0.75, "freq": 1}]
        ),
    }
    assert svc._did_you_mean_from_suggest("melamchee", suggest) == "melamchi"


def test_did_you_mean_falls_back_to_the_romanization_when_no_tag_matches():
    """Authority is a tiebreak, not a filter. ``bhrastachar`` has no curated tag
    within two edits, so the title romanization is the whole of the answer."""
    suggest = {
        "keywords.text": _term_suggestion("bhrastachar", options=[]),
        "title_translit": _term_suggestion(
            "bhrastachar", options=[{"text": "bhrashtacar", "score": 0.82, "freq": 2771}]
        ),
    }
    assert svc._did_you_mean_from_suggest("bhrastachar", suggest) == "bhrashtacar"


def test_did_you_mean_uses_freq_as_the_last_tiebreak_within_one_field():
    """Within a single field, ``freq`` IS a genuine ``P(correction)`` prior — the
    commoner of two equally-close candidates is the better guess."""
    suggest = {
        "keywords.text": _term_suggestion(
            "corupt",
            options=[
                {"text": "corrupz", "score": 0.8, "freq": 2},
                {"text": "corrupt", "score": 0.8, "freq": 900},
            ],
        )
    }
    assert svc._did_you_mean_from_suggest("corupt", suggest) == "corrupt"


def test_did_you_mean_ranks_an_unknown_entry_key_below_every_declared_field():
    """A suggest entry we did not ask for must not win by accident."""
    suggest = {
        "mystery_field": _term_suggestion(
            "melamchee", options=[{"text": "nonsense", "score": 0.99, "freq": 9999}]
        ),
        "keywords.text": _term_suggestion(
            "melamchee", options=[{"text": "melamchi", "score": 0.1, "freq": 1}]
        ),
    }
    assert svc._did_you_mean_from_suggest("melamchee", suggest) == "melamchi"


def test_did_you_mean_keeps_the_tokens_the_suggester_never_looked_at():
    """A mixed query must not be quietly widened into a bare corrected term."""
    client = MagicMock()
    client.search.return_value = _zero_hit_response(
        {
            "title_translit": _term_suggestion(
                "coruption", options=[{"text": "corruption", "score": 0.9}]
            )
        }
    )
    out = SearchService(client=client).search(q="Coruption 2081")
    assert out["did_you_mean"] == "corruption 2081"


def test_did_you_mean_fires_on_a_wholly_fuzzy_result_set():
    """Design §11's SECOND trigger — "only weak matches" — and the one that makes
    the feature reachable at all.

    Gating on ``count == 0`` alone made it nearly dead once §10 landed: bounded
    fuzzy matching rescues most misspellings, so the queries that most need a
    spelling hint (``coruption`` finds 199 real records) stopped qualifying. The
    signal is READ, not inferred: every hit here matched only the named fuzzy
    route, so nothing on the page matched what the reader actually typed.
    """
    client = MagicMock()
    # 3 hits — a NON-empty result set — every one of them a fuzzy rescue.
    response = _matched(_canned_response(), svc.FUZZY_RECALL_CLAUSE_NAME)
    response["suggest"] = {
        "keywords.text": _term_suggestion(
            "coruption", options=[{"text": "corruption", "score": 0.89, "freq": 82}]
        )
    }
    client.search.return_value = response
    out = SearchService(client=client).search(q="coruption")
    assert out["count"] == 3
    # Results AND a suggestion — the reader keeps the hits and learns the spelling.
    assert out["did_you_mean"] == "corruption"
    assert len(out["results"]) == 3


def test_no_did_you_mean_when_an_eligible_token_is_really_indexed():
    """The quiet-on-a-healthy-search guarantee, now stated as the hits state it:
    the page carries at least one EXACT-route match, so the results have a real
    anchor and the suggestion for the neighbouring typo is withheld rather than
    second-guessing good results."""
    client = MagicMock()
    # ``corruption`` matched exactly; the fuzzy route also fired for ``coruption``.
    response = _matched(
        _canned_response(),
        svc.EXACT_RECALL_CLAUSE_NAME,
        svc.FUZZY_RECALL_CLAUSE_NAME,
    )
    response["suggest"] = {
        "keywords.text": _term_suggestion(
            "coruption", options=[{"text": "corruption", "score": 0.89, "freq": 82}]
        ),
    }
    client.search.return_value = response
    out = SearchService(client=client).search(q="corruption coruption")
    assert out["count"] == 3
    assert out["did_you_mean"] is None


def test_no_did_you_mean_for_a_correctly_spelled_query_with_hits():
    """The common case: nothing was missing, so nothing is suggested."""
    client = MagicMock()
    response = _canned_response()  # 3 hits, no suggest block at all
    client.search.return_value = response
    assert SearchService(client=client).search(q="deuba")["did_you_mean"] is None


def test_no_did_you_mean_when_the_only_token_matched_exactly_outside_the_suggest_fields():
    """REGRESSION. The gate used to infer "no exact anchor" from the suggester:
    every eligible token came back corrected, therefore none of them could be
    indexed. That inference was unsound, because ``suggest_mode: "missing"`` is
    evaluated per SUGGEST FIELD and ``SUGGEST_FIELDS`` covers only
    ``keywords.text`` and ``title_translit`` — while the recall route also searches
    ``title_en`` and ``body``.

    So a token living in the OCR ``body`` but absent from both suggest fields
    looked "missing" while matching plenty of documents exactly. Concretely:
    ``minister`` is not a curated tag (the vocabulary has ``ministry``) and is not
    in any title romanization, so the suggester offers ``ministry`` — two edits,
    same first letter, comfortably inside the bounds. It was the only eligible
    token, so the old ``all(...)`` test passed and a correctly spelled query got a
    spurious "did you mean" on top of real results.

    The hits say otherwise, and now the gate listens to them.
    """
    client = MagicMock()
    response = _matched(_canned_response(), svc.EXACT_RECALL_CLAUSE_NAME)
    response["suggest"] = {
        "keywords.text": _term_suggestion(
            "minister", options=[{"text": "ministry", "score": 0.81, "freq": 40}]
        )
    }
    client.search.return_value = response
    out = SearchService(client=client).search(q="minister")
    assert out["count"] == 3
    assert out["did_you_mean"] is None


def test_no_did_you_mean_when_an_ineligible_devanagari_token_anchors_the_results():
    """The mixed-script rough edge the inference could not see, now closed.

    ``देउवा`` is fuzzy-INELIGIBLE, so it never appeared in the eligible-token list
    the old gate quantified over — only ``coruption`` did, and it was corrected, so
    "every eligible token was corrected" held and the offer fired even though the
    Devanagari term was anchoring strong exact results. Reading ``matched_queries``
    sees the anchor regardless of which script produced it.
    """
    client = MagicMock()
    response = _matched(
        _canned_response(),
        svc.EXACT_RECALL_CLAUSE_NAME,
        svc.FUZZY_RECALL_CLAUSE_NAME,
    )
    response["suggest"] = {
        "keywords.text": _term_suggestion(
            "coruption", options=[{"text": "corruption", "score": 0.89, "freq": 82}]
        )
    }
    client.search.return_value = response
    assert SearchService(client=client).search(q="देउवा coruption")["did_you_mean"] is None


def test_weak_match_gate_stays_quiet_when_it_cannot_tell():
    """No names, an unexpected shape, or no hits at all — every one of them means
    the question cannot be answered, and silence is the safe direction. An absent
    suggestion is invisible; a wrong one argues with results already on screen."""
    suggest = {
        "keywords.text": _term_suggestion(
            "coruption", options=[{"text": "corruption", "score": 0.89, "freq": 82}]
        )
    }
    for tag in (None, "exact_recall", 42, {"matched": True}):
        client = MagicMock()
        response = _canned_response()
        for hit in response["hits"]["hits"]:
            if tag is not None:
                hit["matched_queries"] = tag
        response["suggest"] = suggest
        client.search.return_value = response
        out = SearchService(client=client).search(q="coruption")
        assert out["did_you_mean"] is None, tag


def test_weak_match_gate_reads_the_dict_shape_of_matched_queries():
    """OpenSearch returns ``matched_queries`` as a name list, but as a
    ``name -> score`` MAP when match scores are requested. The keys are the same
    names, so the gate must not care which shape arrived."""
    client = MagicMock()
    response = _canned_response()
    for hit in response["hits"]["hits"]:
        hit["matched_queries"] = {svc.FUZZY_RECALL_CLAUSE_NAME: 0.31}
    response["suggest"] = {
        "keywords.text": _term_suggestion(
            "coruption", options=[{"text": "corruption", "score": 0.89, "freq": 82}]
        )
    }
    client.search.return_value = response
    assert SearchService(client=client).search(q="coruption")["did_you_mean"] == "corruption"


def _wholly_fuzzy_response():
    """A non-empty page on which every hit was rescued by the fuzzy route."""
    response = _matched(_canned_response(), svc.FUZZY_RECALL_CLAUSE_NAME)
    response["suggest"] = {
        "keywords.text": _term_suggestion(
            "coruption", options=[{"text": "corruption", "score": 0.89, "freq": 82}]
        )
    }
    return response


def test_weak_match_trigger_is_confined_to_the_first_page():
    """``matched_queries`` describes the PAGE, and relevance puts the exact matches
    first — so paging INTO a healthy result set eventually reaches hits only the
    fuzzy route rescued. Offering there would make the suggestion absent on page 1
    and present on page 4 of the same search, which reads as a glitch. The page-1
    result is the control: same payload, offer made."""
    client = MagicMock()
    client.search.return_value = _wholly_fuzzy_response()
    service = SearchService(client=client)
    assert service.search(q="coruption")["did_you_mean"] == "corruption"
    assert service.search(q="coruption", page=4)["did_you_mean"] is None
    cursor = encode_cursor([1.0, "https://jawafdehi.org/entity/person/deuba"])
    assert service.search(q="coruption", cursor=cursor)["did_you_mean"] is None


def test_weak_match_trigger_needs_a_relevance_sort():
    """Under ``newest``/``oldest``/``title``/``featured`` the score orders nothing,
    so "the exact matches come first" — the property that lets one page speak for
    the whole result set — does not hold and the page cannot be read."""
    client = MagicMock()
    client.search.return_value = _wholly_fuzzy_response()
    service = SearchService(client=client)
    for sort in ("newest", "oldest", "title", "featured"):
        assert service.search(q="coruption", sort=sort)["did_you_mean"] is None, sort
    assert service.search(q="coruption", sort=svc.SORT_RELEVANCE)["did_you_mean"] == (
        "corruption"
    )


def test_zero_result_trigger_survives_paging_and_sort():
    """The other trigger reads the TOTAL, not the page, so neither an offset nor a
    sort mode can suppress it — an empty result is empty on every page."""
    client = MagicMock()
    client.search.return_value = _zero_hit_response(
        {
            "keywords.text": _term_suggestion(
                "coruption", options=[{"text": "corruption", "score": 0.9, "freq": 12}]
            )
        }
    )
    service = SearchService(client=client)
    assert service.search(q="coruption", page=3)["did_you_mean"] == "corruption"
    assert service.search(q="coruption", sort="newest")["did_you_mean"] == "corruption"


def test_one_exact_hit_among_fuzzy_ones_is_enough_to_stay_quiet():
    """"Only weak matches" means NO anchor, not "mostly fuzzy". A single genuine
    match on the page is enough for the results to stand on their own."""
    client = MagicMock()
    response = _matched(_canned_response(), svc.FUZZY_RECALL_CLAUSE_NAME)
    response["hits"]["hits"][1]["matched_queries"] = [svc.EXACT_RECALL_CLAUSE_NAME]
    response["suggest"] = {
        "keywords.text": _term_suggestion(
            "coruption", options=[{"text": "corruption", "score": 0.89, "freq": 82}]
        )
    }
    client.search.return_value = response
    assert SearchService(client=client).search(q="coruption")["did_you_mean"] is None


def test_did_you_mean_is_none_when_nothing_was_suggested():
    """``melamchee`` is three edits from the indexed ``melamci`` — beyond the bound.
    The empty state stays an empty state rather than inventing a correction."""
    client = MagicMock()
    client.search.return_value = _zero_hit_response(
        {"title_translit": _term_suggestion("melamchee", options=[])}
    )
    assert SearchService(client=client).search(q="melamchee")["did_you_mean"] is None


def test_did_you_mean_is_none_when_the_suggestion_equals_the_query():
    """A suggestion identical to what was typed is not a suggestion."""
    client = MagicMock()
    client.search.return_value = _zero_hit_response(
        {
            "title_translit": _term_suggestion(
                "deuba", options=[{"text": "deuba", "score": 1.0}]
            )
        }
    )
    assert SearchService(client=client).search(q="Deuba")["did_you_mean"] is None


def test_did_you_mean_key_is_always_present():
    """Same contract as ``next_cursor``: the key never disappears, so a client can
    read it without probing the envelope's shape."""
    client = MagicMock()
    client.search.return_value = _canned_response()
    out = SearchService(client=client).search(q="x")  # ineligible, no suggester
    assert "did_you_mean" in out
    assert out["did_you_mean"] is None
    browse = SearchService(client=client).search(q="")
    assert browse["did_you_mean"] is None


def test_did_you_mean_parses_defensively_and_never_raises():
    """This rides the happy path of a SUCCESSFUL search, so a malformed or absent
    suggest block must degrade to None rather than turn a 200 into a 500."""
    for malformed in (
        None,
        [],
        "nonsense",
        {},
        {"title_translit": "not-a-list"},
        {"title_translit": [None, 7]},
        {"title_translit": [{"text": "coruption"}]},  # no options key
        {"title_translit": [{"options": [{"text": "x"}]}]},  # no text key
        {"title_translit": _term_suggestion("coruption", options=["not-a-dict"])},
        {"title_translit": _term_suggestion("coruption", options=[{"freq": 3}])},
        {"title_translit": _term_suggestion("coruption", options=[{"text": ""}])},
    ):
        assert svc._did_you_mean_from_suggest("coruption", malformed) is None, malformed


def test_did_you_mean_survives_an_option_with_no_score():
    """Defensive parsing must not swing the other way and DROP a usable option."""
    suggest = {
        "title_translit": _term_suggestion(
            "coruption", options=[{"text": "corruption"}]
        )
    }
    assert svc._did_you_mean_from_suggest("coruption", suggest) == "corruption"
