"""ciaa_press_releases port: the press-release → Material JSON-LD shaper (pure).

The critical property is IDEMPOTENCY with the legacy sync path: the ``@id`` this
shaper mints must equal the one the retired ``sync_materials_from_index`` command
derived from the index document_id ``ngm:ciaa-press-release:<id>`` — otherwise the
go-forward writer forks a duplicate row from the already-synced historical corpus.
That command is gone, but the rows it wrote ARE the live corpus, so the IRI scheme
asserted here is still load-bearing. The doc must also validate under the material
contract.
"""

from jawafdehi_shared.entities.ids import is_valid_material_iri
from materials.jsonld import (
    MaterialType,
    _manuscript_material_iri,
    validate_material_jsonld,
)
from materials.models import Material
from materials.sourcing.ciaa.parse import ParsedPressRelease
from materials.sourcing.ciaa.shaper import (
    CIAA_PRESS_SOURCE,
    press_release_iri,
    press_release_to_jsonld,
)


def _record(**over):
    base = dict(
        press_id=3540,
        title="भ्रष्टाचार मुद्दा दायर सम्बन्धी प्रेस विज्ञप्ति",
        full_text="मिति २०८१।०९।२८ गते आयोगले मुद्दा दायर गरेको ।",
        publication_date_bs="2081-09-28",
        file_urls=["https://ciaa.gov.np/uploads//pressRelease/abc.pdf"],
        source_url="https://ciaa.gov.np/pressrelease/3540",
    )
    base.update(over)
    return ParsedPressRelease(**base)


def test_iri_matches_legacy_index_scheme():
    # THE idempotency anchor: same @id as the frozen-index sync path.
    iri = press_release_iri(3540)
    assert iri == "https://jawafdehi.org/material/ciaa_press_release/3540"
    assert iri == _manuscript_material_iri("ngm:ciaa-press-release:3540")
    assert is_valid_material_iri(iri)


def test_shaper_core_shape():
    doc, material_type = press_release_to_jsonld(_record())
    assert material_type == MaterialType.PRESS_RELEASE
    assert doc["@id"] == "https://jawafdehi.org/material/ciaa_press_release/3540"
    assert doc["@type"] == "CreativeWork"
    assert doc["additionalType"] == "jawafdehi:PressRelease"
    assert doc["name"] == {"ne": "भ्रष्टाचार मुद्दा दायर सम्बन्धी प्रेस विज्ञप्ति"}
    assert doc["jawafdehi:sourceType"] == "CIAA_PRESS_RELEASE"
    assert doc["identifier"] == "ngm:ciaa-press-release:3540"
    assert doc["publisher"]["@type"] == "GovernmentOrganization"


def test_shaper_dates_bs_and_ad():
    doc, _ = press_release_to_jsonld(_record())
    assert doc["jawafdehi:datePublishedBS"] == "2081-09-28"
    # 2081-09-28 BS → 2025-01-12 AD (schema.org datePublished, what search sorts on).
    assert doc["datePublished"] == "2025-01-12"


def test_shaper_body_and_source_page():
    doc, _ = press_release_to_jsonld(_record())
    assert doc["text"]["ne"].startswith("मिति")
    media = doc["associatedMedia"]
    assert any(m.get("jawafdehi:linkRole") == "SOURCE_PAGE" for m in media)


def test_shaper_falls_back_to_synthetic_title_and_bs_only_date():
    # No title, an unconvertible BS date → name falls back, datePublished keeps BS.
    doc, _ = press_release_to_jsonld(_record(title="", publication_date_bs="9999-99-99"))
    assert doc["name"] == {"ne": "CIAA press release 3540"}
    assert doc["jawafdehi:datePublishedBS"] == "9999-99-99"
    assert doc["datePublished"] == "9999-99-99"


def test_shaper_no_date_omits_date_fields():
    doc, _ = press_release_to_jsonld(_record(publication_date_bs=""))
    assert "datePublished" not in doc
    assert "jawafdehi:datePublishedBS" not in doc


def test_produced_doc_validates_and_loads():
    doc, material_type = press_release_to_jsonld(_record())
    # Both the JSON-LD contract check and the ORM promotion must accept it.
    validate_material_jsonld(doc)
    material = Material.from_jsonld(doc, material_type=material_type)
    assert material.source == CIAA_PRESS_SOURCE
    assert material.ident == "3540"
    assert material.material_type == MaterialType.PRESS_RELEASE
