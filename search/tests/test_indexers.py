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
        offence_type="CORRUPTION",
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
    """A published case with the full render-payload attributes set.

    ``status`` stands in for the ``Case.status`` PROPERTY (derived on read from
    the stage list plus the accused outcomes) — a stand-in carries it as a plain
    attribute, which is all ``build_doc`` ever reads.
    """
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
        offence_type="CORRUPTION",
        court_cases=[],
        status="ongoing",
        case_track="ciaa",
        dates={"stages": [{"stage": "initial", "start": "2024-01-01"}]},
        proceedings_started_on=date(2024, 1, 1),
        proceedings_decided_on=None,
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
    assert card["offence_type"] == "CORRUPTION"
    # DEPRECATED alias the deployed SPA card still reads.
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


# ── the stage design: sort key, status, track, derived dates ──────────────────


def test_case_build_doc_date_is_the_derived_proceeding_start():
    """The archive sort key is the earliest start among COURT stages."""
    from datetime import date

    doc = case_index.build_doc(_card_case(proceedings_started_on=date(2024, 3, 4)))
    assert doc["date"] == "2024-03-04"


def test_case_build_doc_date_falls_back_to_the_created_date():
    """A case whose every court stage lacks a start legitimately has no
    proceeding start — a verdict date is often known when the registration date
    is not. It must stay sortable, so the fallback survives the move."""
    from datetime import datetime

    doc = case_index.build_doc(
        _card_case(proceedings_started_on=None, created_at=datetime(2023, 5, 6, 7, 8))
    )
    assert doc["date"] == "2023-05-06"


def test_case_build_doc_date_no_longer_reads_the_legacy_date_pair():
    """The legacy pair is not consulted at all any more.

    It holds the same value (the backfill seeds the ``initial`` stage's start
    from ``case_start_date``), so this is invisible on a migrated row -- but
    reading BOTH would leave two writable surfaces disagreeing the moment a
    caseworker edits the stage list and nothing rewrites the old column.
    """
    from datetime import date

    doc = case_index.build_doc(
        _card_case(
            proceedings_started_on=None,
            case_start_date=date(2019, 1, 1),
            created_at=None,
        )
    )
    assert "date" not in doc


def test_case_build_doc_emits_the_full_new_status_vocabulary():
    """The new field the frontend migrates ONTO: all six lifecycles, verbatim."""
    for value in (
        "ongoing",
        "under_investigation",
        "concluded",
        "others",
        "withdrawn",
        "dormant",
    ):
        assert case_index.build_doc(_card_case(status=value))["status"] == value


def test_case_build_doc_down_maps_every_status_to_the_legacy_facet():
    """Every arm pinned. ``case_status`` is what the DEPLOYED SPA filters on and
    its vocabulary is exactly three values — a new one reaching it would produce
    a facet bucket no deployed client can select."""
    expected = {
        "ongoing": "ongoing",
        "concluded": "closed",
        "withdrawn": "closed",
        "under_investigation": "others",
        "dormant": "others",
        "others": "others",
    }
    for new_value, legacy in expected.items():
        doc = case_index.build_doc(_card_case(status=new_value))
        assert doc["case_status"] == legacy, new_value
        assert doc["status"] == new_value, new_value
    # No arm of the shipped map left unpinned by this test.
    assert expected == case_index.LEGACY_CASE_STATUS


def test_the_legacy_facet_vocabulary_is_exactly_three_values():
    assert set(case_index.LEGACY_CASE_STATUS.values()) == {"ongoing", "closed", "others"}


def test_case_build_doc_down_maps_an_unmapped_status_to_others():
    """A lifecycle added to the model but not to the map must not leak into the
    three-value facet; it still reaches the new ``status`` field verbatim."""
    doc = case_index.build_doc(_card_case(status="teleported"))
    assert doc["case_status"] == "others"
    assert doc["status"] == "teleported"


def test_case_build_doc_status_defaults_to_others_without_the_attribute():
    """``build_doc`` shapes whatever it is handed, including records predating
    the field. ``others`` is the honest bucket, and it costs the case nothing."""
    doc = case_index.build_doc(_published_case())
    assert doc["status"] == "others"
    assert doc["case_status"] == "others"


def test_case_build_doc_status_survives_a_raising_property():
    """``Case.status`` reads the accused binds, so it can raise on an unsaved or
    detached instance. Drop to ``others``, never lose the document."""

    class _Exploding:
        state = "PUBLISHED"
        public_iri = "https://jawafdehi.org/case/x"
        slug = "x"
        title = "X"

        @property
        def status(self):
            raise ValueError("instance needs a primary key")

    doc = case_index.build_doc(_Exploding())
    assert doc["status"] == "others"
    assert doc["case_status"] == "others"
    assert doc["iri"] == "https://jawafdehi.org/case/x"


def test_case_build_doc_mirrors_the_legacy_status_into_raw_and_the_card():
    """Both are the deployed SPA's read paths (``extra.case_status`` and the
    card badge), so both keep the OLD vocabulary."""
    doc = case_index.build_doc(_card_case(status="concluded"))
    assert doc["raw"]["case_status"] == "closed"
    assert doc["raw"]["card"]["status"] == "closed"


def test_case_build_doc_indexes_case_track():
    assert case_index.build_doc(_card_case(case_track="ciaa"))["case_track"] == "ciaa"


def test_case_build_doc_omits_case_track_when_unset():
    """Null is the honest value for the ~2,900 unclassified drafts. An empty
    keyword would give the facet a nameless bucket holding all of them."""
    for unset in (None, "", "   "):
        assert "case_track" not in case_index.build_doc(_card_case(case_track=unset))
    assert "case_track" not in case_index.build_doc(_published_case())


def test_case_build_doc_indexes_the_derived_proceeding_dates():
    from datetime import date

    doc = case_index.build_doc(
        _card_case(
            proceedings_started_on=date(2024, 2, 25),
            proceedings_decided_on=date(2024, 5, 22),
        )
    )
    assert doc["proceedings_started_on"] == "2024-02-25"
    assert doc["proceedings_decided_on"] == "2024-05-22"


def test_case_build_doc_omits_the_proceeding_dates_when_null():
    """A null against a ``date`` mapping is rejected and takes the WHOLE
    document with it; an absent field is simply excluded by a range clause,
    which is right for a case with no start or no decision yet."""
    doc = case_index.build_doc(
        _card_case(proceedings_started_on=None, proceedings_decided_on=None)
    )
    assert "proceedings_started_on" not in doc
    assert "proceedings_decided_on" not in doc
    assert "proceedings_started_on" not in case_index.build_doc(_published_case())


def test_case_build_doc_keeps_the_stage_list_out_of_the_indexed_fields():
    """The stage document is DISPLAY-only. Promoting it would make the shape of
    a JSON blob part of the query contract, and OpenSearch would dynamically map
    every key inside it."""
    doc = case_index.build_doc(_card_case())
    assert "dates" not in doc
    assert "stages" not in doc
    assert doc["raw"]["card"]["stages"] == [{"stage": "initial", "start": "2024-01-01"}]


def test_case_build_doc_drops_non_dict_stage_records():
    doc = case_index.build_doc(
        _card_case(dates={"stages": [{"stage": "initial"}, "not-a-dict", None]})
    )
    assert doc["raw"]["card"]["stages"] == [{"stage": "initial"}]


def test_case_build_doc_survives_a_malformed_stage_document():
    """A legacy row migrated unchanged must still render, not abort a reindex."""
    for junk in (None, {}, [], "nope", {"stages": None}, {"stages": "nope"}):
        doc = case_index.build_doc(_card_case(dates=junk))
        assert doc["raw"]["card"]["stages"] == [], junk


def test_case_build_doc_keeps_both_case_type_and_offence_type():
    """``case_type`` shares ONE facet bucket with the NGM courtcase docs on
    purpose; ``offence_type`` is additive. Neither is touched by this change."""
    doc = case_index.build_doc(_card_case())
    assert doc["case_type"] == "CORRUPTION"
    assert doc["offence_type"] == "CORRUPTION"
    assert "CORRUPTION" in doc["keywords"]
    assert doc["raw"]["case_type"] == "CORRUPTION"
    assert doc["raw"]["offence_type"] == "CORRUPTION"
    assert doc["raw"]["card"]["case_type"] == "CORRUPTION"
    assert doc["raw"]["card"]["offence_type"] == "CORRUPTION"
