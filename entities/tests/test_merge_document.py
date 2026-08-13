"""Reference rewriting and self-reference dropping (pure logic, no DB)."""

from entities.services.merge.document import drop_self_references, rewrite_references

JHAPA = "https://jawafdehi.org/entity/location/district/jhapa-np0104"
LOOSE = "https://jawafdehi.org/entity/location/jhapa"
KOSHI = "https://jawafdehi.org/entity/location/province/koshi-np01"


def test_identity_and_provenance_are_never_dropped():
    doc = {
        "@id": JHAPA,
        "@type": "AdministrativeArea",
        "@context": "https://schema.org",
        "dateCreated": "2026-07-15T06:51:44Z",
        "jawafdehi:version": {"version_number": 1},
    }
    out, count = drop_self_references(doc, {LOOSE})
    assert out == doc
    assert count == 0


def test_a_retired_iri_in_sameas_is_dropped_too():
    # The merge no longer records what it retired in sameAs, so nothing exempts the
    # field any more — a stale pointer there is a reference like any other.
    doc = {"@id": JHAPA, "@type": "AdministrativeArea", "sameAs": [LOOSE, KOSHI]}
    out, count = drop_self_references(doc, {LOOSE})
    assert out["sameAs"] == [KOSHI]
    assert count == 1


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


def test_a_bare_string_reference_is_never_rewritten():
    # count_references depends on this: a bare-string mention is not repointable.
    doc = {"@id": KOSHI, "sameAs": [LOOSE]}
    out, count = rewrite_references(doc, {LOOSE: JHAPA})
    assert out["sameAs"] == [LOOSE]
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


def test_an_already_empty_list_is_left_alone():
    doc = {"@id": JHAPA, "@type": "Place", "keywords": []}
    out, count = drop_self_references(doc, {LOOSE})
    assert out["keywords"] == []
    assert count == 0


def test_an_object_emptied_by_the_drop_is_removed_entirely():
    doc = {"@id": JHAPA, "@type": "Place", "deep": {"inner": {"@id": LOOSE}}}
    out, count = drop_self_references(doc, {LOOSE})
    assert "deep" not in out
    assert count == 1
