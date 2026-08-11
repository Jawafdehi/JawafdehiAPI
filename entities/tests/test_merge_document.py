"""Document merging + recursive reference rewriting (pure logic, no DB)."""

from entities.services.merge.document import merge_documents, rewrite_references

JHAPA = "https://jawafdehi.org/entity/location/district/jhapa-np0104"
LOOSE = "https://jawafdehi.org/entity/location/jhapa"
KOSHI = "https://jawafdehi.org/entity/location/province/koshi-np01"


def test_survivor_wins_a_scalar_conflict():
    survivor = {"@id": JHAPA, "@type": "AdministrativeArea", "name": {"en": "Jhapa"}}
    dup = {"@id": LOOSE, "@type": "Place", "name": {"en": "Jhapa District"}}
    merged, inherited = merge_documents(survivor, [dup])
    assert merged["name"] == {"en": "Jhapa"}
    assert "name" not in inherited


def test_gap_on_the_survivor_is_filled_from_the_duplicate():
    survivor = {"@id": JHAPA, "@type": "AdministrativeArea", "name": {"en": "Jhapa"}}
    dup = {"@id": LOOSE, "@type": "Place", "description": {"ne": "झापा जिल्ला"}}
    merged, inherited = merge_documents(survivor, [dup])
    assert merged["description"] == {"ne": "झापा जिल्ला"}
    assert inherited["description"] == LOOSE


def test_list_fields_are_unioned_and_deduplicated():
    survivor = {"@id": JHAPA, "@type": "AdministrativeArea", "keywords": ["district", "koshi"]}
    dup = {"@id": LOOSE, "@type": "Place", "keywords": ["koshi", "terai"]}
    merged, _ = merge_documents(survivor, [dup])
    assert merged["keywords"] == ["district", "koshi", "terai"]


def test_retired_iris_are_recorded_as_sameas():
    survivor = {"@id": JHAPA, "@type": "AdministrativeArea"}
    merged, _ = merge_documents(survivor, [{"@id": LOOSE, "@type": "Place"}])
    assert LOOSE in merged["sameAs"]


def test_identity_and_provenance_are_never_inherited():
    survivor = {"@id": JHAPA, "@type": "AdministrativeArea"}
    dup = {
        "@id": LOOSE,
        "@type": "Place",
        "@context": "https://schema.org",
        "dateCreated": "2026-07-15T06:51:44Z",
        "jawafdehi:version": {"version_number": 1},
    }
    merged, inherited = merge_documents(survivor, [dup])
    assert merged["@id"] == JHAPA
    assert merged["@type"] == "AdministrativeArea"
    assert "dateCreated" not in merged
    assert "jawafdehi:version" not in merged
    assert inherited == {}


def test_rewrites_a_nested_reference_at_any_depth():
    doc = {
        "@id": "https://jawafdehi.org/entity/location/localunit/damak-10502",
        "containedInPlace": {"@id": LOOSE},
        "jawafdehi:custom": {"deeper": [{"nested": {"@id": LOOSE}}]},
    }
    out, count = rewrite_references(doc, {LOOSE: JHAPA})
    assert out["containedInPlace"]["@id"] == JHAPA
    assert out["jawafdehi:custom"]["deeper"][0]["nested"]["@id"] == JHAPA
    assert count == 2


def test_rewriting_never_touches_the_version_provenance_block():
    doc = {"@id": KOSHI, "jawafdehi:version": {"entity_iri": LOOSE}}
    out, count = rewrite_references(doc, {LOOSE: JHAPA})
    assert out["jawafdehi:version"]["entity_iri"] == LOOSE
    assert count == 0


def test_rewriting_never_touches_the_documents_own_id():
    doc = {"@id": LOOSE, "name": {"en": "Jhapa"}}
    out, count = rewrite_references(doc, {LOOSE: JHAPA})
    assert out["@id"] == LOOSE
    assert count == 0


def test_a_list_holding_both_collapses_to_one():
    doc = {"@id": KOSHI, "about": [{"@id": JHAPA}, {"@id": LOOSE}]}
    out, count = rewrite_references(doc, {LOOSE: JHAPA})
    assert out["about"] == [{"@id": JHAPA}]
    assert count == 1


def test_rewrite_leaves_an_unrelated_document_untouched():
    doc = {"@id": KOSHI, "containedInPlace": {"@id": "https://jawafdehi.org/entity/location/nepal"}}
    out, count = rewrite_references(doc, {LOOSE: JHAPA})
    assert out == doc
    assert count == 0


def test_an_untouched_list_keeps_its_pre_existing_duplicates():
    doc = {"@id": KOSHI, "tags": ["a", "a"]}
    out, count = rewrite_references(doc, {LOOSE: JHAPA})
    assert out["tags"] == ["a", "a"]
    assert count == 0
