"""build_doc shape tests for the four per-app unified-search indexers.

Pure-Python: build_doc operates on plain objects with attributes, so these tests
need NO database and NO live OpenSearch. They assert the common-doc field shape
per type, the bilingual title extraction, and the case-only-published rule.
"""

from __future__ import annotations

from types import SimpleNamespace

from cases import search_index as case_index
from entities import search_index as entity_index
from courts import search_index as courtcase_index
from materials import search_index as material_index

COMMON_FIELDS = {
    "iri",
    "type",
    "source_app",
    "title_ne",
    "title_en",
    "title_translit",
    "body",
    "keywords",
    "identifiers",
    "raw",
}


# ── entity ───────────────────────────────────────────────────────────────────


def test_entity_build_doc_shape():
    iri = "https://jawafdehi.org/entity/person/sher-bahadur-deuba"
    obj = SimpleNamespace(
        iri=iri,
        data={
            "@id": iri,
            "@type": "Person",
            "name": {"ne": "शेर बहादुर देउवा", "en": "Sher Bahadur Deuba"},
            "keywords": ["politician", "prime-minister"],
            "description": {"en": "Former PM"},
            "identifier": "NP-001",
        },
    )
    doc = entity_index.build_doc(obj)
    assert COMMON_FIELDS <= set(doc)
    assert doc["iri"] == iri
    assert doc["type"] == "Person"
    assert doc["source_app"] == "nes"
    assert doc["title_ne"] == "शेर बहादुर देउवा"
    assert doc["title_en"] == "Sher Bahadur Deuba"
    assert doc["keywords"] == ["politician", "prime-minister"]
    assert iri in doc["identifiers"]
    assert "NP-001" in doc["identifiers"]
    assert "Former PM" in doc["body"]
    # title_translit carries a romanization of the Devanagari title.
    assert doc["title_translit"]


def test_entity_list_type_joined():
    iri = "https://jawafdehi.org/entity/location/kathmandu"
    obj = SimpleNamespace(
        iri=iri,
        data={"@id": iri, "@type": ["Place", "AdministrativeArea"], "name": "Kathmandu"},
    )
    doc = entity_index.build_doc(obj)
    assert doc["type"] == "Place,AdministrativeArea"
    # Plain Latin string name → title_en, not title_ne.
    assert doc["title_en"] == "Kathmandu"
    assert doc["title_ne"] is None


# ── material ───────────────────────────────────────────────────────────────────


def test_material_build_doc_shape_with_dates():
    iri = "https://jawafdehi.org/material/court/supreme.081-cr-0081"
    obj = SimpleNamespace(
        iri=iri,
        ident="supreme.081-cr-0081",
        source="court",
        data={
            "@id": iri,
            "@type": ["Manuscript", "DigitalDocument"],
            "name": {"ne": "आदेश"},
            "text": {"ne": "पूरा पाठ यहाँ"},
            "keywords": ["court-order"],
            "datePublished": "2024-01-15",
            "jawafdehi:registrationDateBS": "2080-10-01",
            "identifier": "081-CR-0081",
        },
    )
    doc = material_index.build_doc(obj)
    assert COMMON_FIELDS <= set(doc)
    assert doc["iri"] == iri
    assert doc["type"] == "Manuscript,DigitalDocument"
    assert doc["source_app"] == "ngm"
    assert doc["title_ne"] == "आदेश"
    assert "पूरा पाठ यहाँ" in doc["body"]
    assert doc["date"] == "2024-01-15"
    # Bikram Sambat carried verbatim (never coerced to a date).
    assert doc["date_bs"] == "2080-10-01"
    assert "081-CR-0081" in doc["identifiers"]


def test_material_build_doc_promotes_the_provenance_facets():
    """``material_type``/``source`` become top-level facet fields.

    ``source`` is indexed as ``material_source``: the doc already carries
    ``source_app`` (the owning application, "ngm"), and two keys a letter apart
    meaning different things would be a trap for whoever writes the next query.
    """
    iri = "https://jawafdehi.org/material/ciaa_press_release/1701"
    obj = SimpleNamespace(
        iri=iri,
        ident="1701",
        source="ciaa_press_release",
        material_type="press_release",
        data={"@id": iri, "@type": "CreativeWork", "name": {"ne": "प्रेस विज्ञप्ति"}},
    )
    doc = material_index.build_doc(obj)
    assert doc["material_type"] == "press_release"
    assert doc["material_source"] == "ciaa_press_release"
    # The application tag is a separate field and keeps its own value.
    assert doc["source_app"] == "ngm"
    assert "source" not in doc


def test_material_build_doc_omits_blank_provenance():
    """A blank column is left OUT, not written as "".

    A ``terms`` filter excludes a doc that lacks the field, which is what an
    unrecorded source should do — an "" bucket would otherwise show up in the
    facet as a nameless option a reader could select.
    """
    iri = "https://jawafdehi.org/material/x/1"
    obj = SimpleNamespace(
        iri=iri,
        ident="1",
        source="",
        material_type=None,
        data={"@id": iri, "@type": "CreativeWork"},
    )
    doc = material_index.build_doc(obj)
    assert "material_type" not in doc
    assert "material_source" not in doc


# ── courtcase ──────────────────────────────────────────────────────────────────


def _courtcase_obj():
    # A bare CourtCase-like object: ``.iri`` uses build_courtcase_iri from the
    # composite key; party lookups degrade gracefully with no DB.
    from jawafdehi_shared.entities.ids import build_courtcase_iri

    obj = SimpleNamespace(
        court_id="supreme",
        case_number="081-CR-0081",
        case_type="corruption",
        case_status="decided",
        plaintiff="नेपाल सरकार",
        defendant="राम बहादुर",
        nes_id="https://jawafdehi.org/entity/person/ram-bahadur",
        registration_date_ad=None,
        registration_date_bs="2080-10-01",
        court=SimpleNamespace(full_name_english="Supreme Court", court_type="supreme"),
    )
    obj.iri = build_courtcase_iri(obj.court_id, obj.case_number)
    return obj


def test_courtcase_build_doc_shape_and_title_from_case_number():
    obj = _courtcase_obj()
    doc = courtcase_index.build_doc(obj)
    assert COMMON_FIELDS <= set(doc)
    assert doc["source_app"] == "ngm"
    assert doc["type"] == "jawafdehi:CourtCase"
    assert doc["iri"].endswith("/courtcase/supreme/081-cr-0081")
    # CONTRACT GAP: court cases have no language-map name. title_ne is the
    # case_number; title_en is the English court name + number.
    assert doc["title_ne"] == "081-CR-0081"
    assert doc["title_en"] == "Supreme Court 081-CR-0081"
    # Party names flow into body + keywords so a party-name query matches.
    assert "नेपाल सरकार" in doc["body"]
    assert "राम बहादुर" in doc["keywords"]
    # case_number, court, and the case-level nes_id ride in identifiers.
    assert "081-CR-0081" in doc["identifiers"]
    assert "supreme" in doc["identifiers"]
    assert "https://jawafdehi.org/entity/person/ram-bahadur" in doc["identifiers"]
    assert doc["date_bs"] == "2080-10-01"
    # case_type promoted to a top-level keyword for filtering/faceting, NORMALIZED
    # to upper-case so scraper casing variants ("corruption"/"Corruption") don't
    # split into duplicate facet buckets. The verbatim value stays in ``raw``.
    assert doc["case_type"] == "CORRUPTION"
    assert doc["raw"]["case_type"] == "corruption"
    # The court identifier promoted to a top-level keyword for the one-court
    # facet — ``identifiers`` and ``raw.court`` carry it too, but neither can
    # aggregate.
    assert doc["court"] == "supreme"
    # Court tier promoted for the unified search's court_type facet.
    assert doc["court_type"] == "supreme"
    # National jurisdiction: no district at all, and the NATIONAL sentinel for
    # province so "national jurisdiction" stays a visible, filterable group.
    assert "court_district" not in doc
    assert doc["court_province"] == "NATIONAL"


def test_courtcase_parties_fall_back_to_the_case_level_strings():
    """No DB here, so ``CaseEntity`` cannot be read — the shaping must still
    attribute a side, using the case's own ``plaintiff``/``defendant``. This is
    also the live path for a case that was never entity-resolved."""
    doc = courtcase_index.build_doc(_courtcase_obj())
    assert doc["raw"]["parties"] == {
        "plaintiff": {"names": ["नेपाल सरकार"], "total": 1},
        "defendant": {"names": ["राम बहादुर"], "total": 1},
    }
    # The flattened bag is unchanged: it feeds text recall, not the card.
    assert "नेपाल सरकार" in doc["keywords"]


def test_courtcase_parties_empty_side_reports_zero_not_a_missing_key():
    """A side with nothing on it stays present with total 0, so a client
    branches on the number rather than on whether a key exists."""
    obj = _courtcase_obj()
    obj.plaintiff = None
    obj.defendant = "   "  # whitespace-only is not a party
    doc = courtcase_index.build_doc(obj)
    assert doc["raw"]["parties"] == {
        "plaintiff": {"names": [], "total": 0},
        "defendant": {"names": [], "total": 0},
    }


def test_courtcase_title_en_none_without_english_court_name():
    obj = _courtcase_obj()
    obj.court = SimpleNamespace(full_name_english=None)
    doc = courtcase_index.build_doc(obj)
    assert doc["title_en"] is None
    assert doc["title_ne"] == "081-CR-0081"


def test_courtcase_court_type_absent_for_stub_court():
    """Scraper stubs create Court rows with ``court_type=""`` — an empty keyword
    would pollute the facet with a nameless bucket, so the field is dropped."""
    obj = _courtcase_obj()
    obj.court = SimpleNamespace(full_name_english=None, court_type="")
    doc = courtcase_index.build_doc(obj)
    assert "court_type" not in doc


def test_courtcase_court_type_absent_without_court():
    """Drop the field, never the document (the bigo lesson)."""
    obj = _courtcase_obj()
    obj.court = None
    doc = courtcase_index.build_doc(obj)
    assert "court_type" not in doc


def test_courtcase_court_type_is_lowercased():
    """One controlled vocabulary — casing variants must not split facet buckets."""
    obj = _courtcase_obj()
    obj.court = SimpleNamespace(full_name_english=None, court_type="District")
    doc = courtcase_index.build_doc(obj)
    assert doc["court_type"] == "district"


def test_courtcase_court_identifier_is_indexed_even_without_a_court_row():
    """The one-court facet is fed from ``court_id``, not the joined Court row, so
    it survives the stub/None cases that drop ``court_type``."""
    obj = _courtcase_obj()
    obj.court_id = "achhamdc"
    obj.court = None
    doc = courtcase_index.build_doc(obj)
    assert doc["court"] == "achhamdc"


def test_courtcase_district_court_resolves_its_own_district_and_province():
    """A district court's identifier IS its scraper code_name; geography is
    derived from it alone (no DB access)."""
    obj = _courtcase_obj()
    obj.court_id = "achhamdc"
    doc = courtcase_index.build_doc(obj)
    assert doc["court_district"] == "Achham"
    assert doc["court_province"] == "Sudurpashchim"


def test_courtcase_high_court_gets_a_province_but_no_district():
    """A high court is a PROVINCIAL court: its seat district would answer "which
    town is the bench in", not "whose case is this" — so only province is
    indexed, and an additional bench resolves to its parent court's province."""
    obj = _courtcase_obj()
    obj.court_id = "patanhc"
    doc = courtcase_index.build_doc(obj)
    assert "court_district" not in doc
    assert doc["court_province"] == "Bagmati"
    # Butwal is an additional bench of High Court Tulsipur — same province.
    obj.court_id = "butwalhc"
    assert courtcase_index.build_doc(obj)["court_province"] == "Lumbini"


def test_courtcase_unknown_court_indexes_no_location():
    """Indexing nothing is recoverable; a wrong bucket is a lie the facet serves."""
    obj = _courtcase_obj()
    obj.court_id = "atlantisdc"
    doc = courtcase_index.build_doc(obj)
    assert "court_district" not in doc
    assert "court_province" not in doc


# ── case ───────────────────────────────────────────────────────────────────────


def _published_case():
    return SimpleNamespace(
        state="PUBLISHED",
        public_iri="https://jawafdehi.org/case/budget-scam-2080",
        slug="budget-scam-2080",
        title="Budget allocation scam",
        description="A detailed markdown description.",
        short_description="Short summary.",
        key_allegations=["Misappropriation of funds", "Forgery"],
        tags=["corruption", "budget"],
        case_type="CORRUPTION",
        court_cases=["https://jawafdehi.org/courtcase/supreme/081-cr-0081"],
        case_start_date=None,
        created_at=None,
        updated_at=None,
    )


def test_case_build_doc_shape_published():
    case = _published_case()
    doc = case_index.build_doc(case)
    assert COMMON_FIELDS <= set(doc)
    assert doc["iri"] == "https://jawafdehi.org/case/budget-scam-2080"
    assert doc["type"] == "Case"
    assert doc["source_app"] == "jawafdehi"
    assert doc["title_en"] == "Budget allocation scam"
    # description + key_allegations fold into body.
    assert "detailed markdown" in doc["body"]
    assert "Misappropriation of funds" in doc["body"]
    # tags + case_type into keywords.
    assert "corruption" in doc["keywords"]
    assert "CORRUPTION" in doc["keywords"]
    # case_type also promoted to a top-level keyword for filtering/faceting.
    assert doc["case_type"] == "CORRUPTION"
    # slug + court_cases refs into identifiers: the stored IRI plus the bare
    # case number for recall (no legacy "<court>:<number>" form).
    assert "budget-scam-2080" in doc["identifiers"]
    assert "https://jawafdehi.org/courtcase/supreme/081-cr-0081" in doc["identifiers"]
    assert "081-cr-0081" in doc["identifiers"]
    assert "supreme:081-cr-0081" not in doc["identifiers"]


def test_case_build_doc_weight_defaults_to_zero_when_absent():
    """build_doc must not REQUIRE ``weight`` — it shapes objects predating the field."""
    assert case_index.build_doc(_published_case())["weight"] == 0


def test_case_build_doc_carries_weight():
    case = _published_case()
    case.weight = 50
    assert case_index.build_doc(case)["weight"] == 50


def test_case_build_doc_weight_coerces_none_to_zero():
    case = _published_case()
    case.weight = None
    assert case_index.build_doc(case)["weight"] == 0


def test_case_should_index_only_published():
    assert case_index.should_index(_published_case()) is True
    for state in ("DRAFT", "IN_REVIEW", "CLOSED"):
        case = _published_case()
        case.state = state
        assert case_index.should_index(case) is False


def _card_case(**overrides):
    """A published case with the full render-payload attributes set."""
    from datetime import date

    base = dict(
        state="PUBLISHED",
        public_iri="https://jawafdehi.org/case/land-grab-2081",
        slug="land-grab-2081",
        title="Land grab",
        description="desc",
        short_description="<b>Short</b> summary.",
        key_allegations=["Encroachment", ""],
        tags=["land", "corruption"],
        case_type="CORRUPTION",
        court_cases=[],
        case_start_date=date(2024, 1, 1),
        case_end_date=None,
        thumbnail_url="https://cdn/thumb.png",
        banner_url="https://cdn/banner.png",
        bigo=12345678,
        timeline=[{"date": "2024-01-01", "title": "Filed"}, "not-a-dict"],
        created_at=None,
        updated_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_case_build_doc_status_ongoing_closed_others():
    from datetime import date

    # Coarse lifecycle rides on the dedicated ``case_status`` keyword (NOT the
    # generic ``status``, which NGM courtcases use for their enrichment flag).
    assert case_index.build_doc(_card_case())["case_status"] == "ongoing"
    assert "status" not in case_index.build_doc(_card_case())
    closed = case_index.build_doc(_card_case(case_end_date=date(2024, 6, 1)))
    assert closed["case_status"] == "closed"
    others = case_index.build_doc(_card_case(case_start_date=None))
    assert others["case_status"] == "others"


def test_case_build_doc_mirrors_case_status_into_raw():
    """``raw.case_status`` must be set so ``_serialize_hit`` can surface it as
    ``extra.case_status`` (the SPA's non-card lifecycle fallback)."""
    doc = case_index.build_doc(_card_case())
    assert doc["raw"]["case_status"] == doc["case_status"] == "ongoing"


def test_build_indexed_doc_resolves_entities_for_bulk_path(monkeypatch):
    """The bulk/live wrapper resolves NES names so a rebuild refreshes (not blanks)
    the card entities — the pure build_doc alone would leave them empty."""
    rel = SimpleNamespace(
        nes_id="https://jawafdehi.org/entity/person/x",
        relationship_type="accused",
        outcome="convicted",
        notes="",
    )
    case = _card_case()
    case.entity_relationships = SimpleNamespace(all=lambda: [rel])
    monkeypatch.setattr(
        "cases.services.nes_resolver.resolve_entities",
        lambda ids: {rel.nes_id: {"display_name": "Person X", "entity_type": "Person"}},
    )
    doc = case_index.build_indexed_doc(case)
    assert doc["raw"]["card"]["entities"][0]["display_name"] == "Person X"
    # Pure build_doc (what the driver would call without the wrapper) leaves it empty.
    assert case_index.build_doc(case)["raw"]["card"]["entities"] == []


def test_case_build_doc_card_payload():
    entities = [
        {
            "nes_id": "https://jawafdehi.org/entity/person/x",
            "display_name": "Person X",
            "entity_type": "Person",
            "type": "accused",
            "outcome": "convicted",
            "notes": "",
        }
    ]
    doc = case_index.build_doc(_card_case(), entities=entities)
    card = doc["raw"]["card"]
    # Render fields the SPA card needs, denormalized into the doc.
    assert card["slug"] == "land-grab-2081"
    assert card["short_description"] == "<b>Short</b> summary."
    assert card["key_allegations"] == ["Encroachment"]  # blank dropped
    assert card["tags"] == ["land", "corruption"]
    assert card["case_type"] == "CORRUPTION"
    assert card["status"] == "ongoing"
    assert card["case_start_date"] == "2024-01-01"
    assert card["case_end_date"] is None
    assert card["bigo"] == 12345678
    assert card["thumbnail_url"] == "https://cdn/thumb.png"
    assert card["banner_url"] == "https://cdn/banner.png"
    # timeline (major events) carried verbatim; non-dict entries filtered out.
    assert card["timeline"] == [{"date": "2024-01-01", "title": "Filed"}]
    # resolved entity binds (with per-entity outcome) ride along.
    assert card["entities"] == entities


def test_case_build_doc_card_entities_default_empty():
    """Pure shaping (no ``entities`` arg) still emits a card, with no entities."""
    doc = case_index.build_doc(_card_case())
    assert doc["raw"]["card"]["entities"] == []


# ── बिगो promoted to a top-level range-queryable field ─────────────────────────


def test_case_build_doc_promotes_bigo_to_top_level():
    """बिगो must be a TOP-LEVEL field, not only the card copy.

    ``raw`` is mapped ``enabled: false``, so ``raw.card.bigo`` is stored but never
    indexed — a range filter can only see the promoted field. Both must be
    present: the promoted one to filter on, the card one to render from.
    """
    doc = case_index.build_doc(_card_case())
    assert doc["bigo"] == 12345678
    assert doc["raw"]["card"]["bigo"] == 12345678


def test_case_build_doc_omits_bigo_when_unrecorded():
    """No amount → the key is ABSENT (not null).

    A ``range`` clause excludes a document missing the field, which is exactly the
    wanted behaviour for a case with no known amount; a ``null`` would instead be
    rejected by the ``long`` mapping and drop the whole case from the index.
    """
    doc = case_index.build_doc(_card_case(bigo=None))
    assert "bigo" not in doc
    # The card still carries its own (null) copy — the SPA renders "not recorded".
    assert doc["raw"]["card"]["bigo"] is None


def test_case_build_doc_coerces_a_numeric_string_bigo():
    """An API-shaped record can carry बिगो as a string; index it as a number."""
    assert case_index.build_doc(_card_case(bigo="66000000000"))["bigo"] == 66000000000
    assert case_index.build_doc(_card_case(bigo="1.9"))["bigo"] == 1


def test_case_build_doc_drops_an_uncoercible_bigo_without_losing_the_case():
    """Garbage in ``bigo`` must cost the FIELD, never the whole document.

    Sending a non-numeric value against the ``long`` mapping would have OpenSearch
    reject the doc outright, silently removing a published case from search.
    """
    junk_values = (
        "not-a-number",
        object(),
        True,  # a bool is not an amount; would otherwise index as 1
        2**63,  # past the ``long`` ceiling — rejected by the mapping
        -(2**63) - 1,
        float("inf"),  # int(float("inf")) raises OverflowError
        float("nan"),  # int(float("nan")) raises ValueError
    )
    for junk in junk_values:
        doc = case_index.build_doc(_card_case(bigo=junk))
        assert "bigo" not in doc, junk
        assert doc["iri"] == "https://jawafdehi.org/case/land-grab-2081"


def test_case_build_doc_keeps_the_largest_real_amount_exactly():
    """The screen must reject only what the ``long`` mapping cannot hold, and must
    not mangle what it keeps.

    The corpus already holds amounts into the tens of अरब. Coercing through
    ``float`` is lossy above 2**53 — it would round the ceiling value UP past the
    ceiling and silently drop a valid figure — so the coercion tries ``int`` first.
    """
    assert case_index.build_doc(_card_case(bigo=66_000_000_000))["bigo"] == 66_000_000_000
    assert case_index.build_doc(_card_case(bigo=2**63 - 1))["bigo"] == 2**63 - 1
    # Same for the string form an API-shaped record carries.
    assert case_index.build_doc(_card_case(bigo=str(2**63 - 1)))["bigo"] == 2**63 - 1


def test_case_build_doc_keeps_a_zero_bigo():
    """``0`` is a recorded amount, not a synonym for "unknown" — so it is indexed
    and remains filterable. (No published case records one; inventing the rule
    here would make an honest zero invisible to every bound.)"""
    assert case_index.build_doc(_card_case(bigo=0))["bigo"] == 0
