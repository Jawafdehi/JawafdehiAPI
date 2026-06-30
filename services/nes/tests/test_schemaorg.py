"""Tests for the JSON-LD validation + authoring-shape normalization.

CLEAN-SLATE remodel: the per-type Pydantic ``to_jsonld`` projection was dropped;
the stored form IS schema.org JSON-LD. These exercise the minimal validator
(``entities.validation``) and the authoring-shape normalizer
(``entities.write_validation``).

Run under the monolith settings from the repo root:
    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest services/nes/tests
"""

from __future__ import annotations

from django.test import SimpleTestCase

from nes_service.entities.validation import (
    JsonLdValidationError,
    primary_type,
    validate_jsonld_entity,
)
from nes_service.entities.write_validation import (
    normalize_authoring_payload,
    validate_create_payload,
)

IRI = "https://jawafdehi.org/entity/person/ram-bahadur"


def _doc(**over):
    base = {"@type": "Person", "@id": IRI, "name": {"en": "Ram Bahadur"}}
    base.update(over)
    return base


class ValidationTests(SimpleTestCase):
    def test_valid_doc_passes(self):
        self.assertEqual(validate_jsonld_entity(_doc()), _doc())

    def test_string_name_ok(self):
        validate_jsonld_entity(_doc(name="Ram Bahadur"))

    def test_list_type_ok(self):
        validate_jsonld_entity(_doc(**{"@type": ["Place", "AdministrativeArea"]}))

    def test_jawafdehi_curie_type_ok(self):
        # A jawafdehi: extension type is a known type.
        validate_jsonld_entity(
            _doc(
                **{
                    "@type": "Organization",
                    "@id": "https://jawafdehi.org/entity/organization/political_party/nc",
                    "additionalType": "jawafdehi:PoliticalParty",
                }
            )
        )

    def test_bad_id_rejected(self):
        with self.assertRaises(JsonLdValidationError):
            validate_jsonld_entity(_doc(**{"@id": "entity:person/ram-bahadur"}))

    def test_non_iri_id_rejected(self):
        with self.assertRaises(JsonLdValidationError):
            validate_jsonld_entity(_doc(**{"@id": "not-an-iri"}))

    def test_unknown_type_rejected(self):
        with self.assertRaises(JsonLdValidationError):
            validate_jsonld_entity(_doc(**{"@type": "Wizard"}))

    def test_missing_name_rejected(self):
        d = _doc()
        del d["name"]
        with self.assertRaises(JsonLdValidationError):
            validate_jsonld_entity(d)

    def test_empty_name_map_rejected(self):
        with self.assertRaises(JsonLdValidationError):
            validate_jsonld_entity(_doc(name={"en": "  "}))

    def test_primary_type_joins_list(self):
        self.assertEqual(primary_type(_doc()), "Person")
        self.assertEqual(
            primary_type(_doc(**{"@type": ["Place", "AdministrativeArea"]})),
            "Place,AdministrativeArea",
        )


class NormalizationTests(SimpleTestCase):
    def test_authoring_shape_builds_jsonld(self):
        doc = normalize_authoring_payload(
            {"prefix": "person", "slug": "ram-bahadur", "type": "Person",
             "name": {"en": "Ram Bahadur"}, "keywords": ["politician"]}
        )
        self.assertEqual(doc["@id"], IRI)
        self.assertEqual(doc["@type"], "Person")
        self.assertIn("https://schema.org", doc["@context"])
        self.assertEqual(doc["keywords"], ["politician"])
        # Authoring-only keys are not copied through.
        self.assertNotIn("prefix", doc)
        self.assertNotIn("slug", doc)
        self.assertNotIn("type", doc)

    def test_entity_prefix_alias(self):
        doc = normalize_authoring_payload(
            {"entity_prefix": "organization/political_party", "slug": "nc",
             "type": "Organization", "name": {"en": "NC"}}
        )
        self.assertEqual(
            doc["@id"], "https://jawafdehi.org/entity/organization/political_party/nc"
        )

    def test_full_jsonld_passthrough(self):
        src = {"@type": "Person", "@id": IRI, "name": {"en": "Ram"}}
        doc = normalize_authoring_payload(src)
        self.assertEqual(doc["@id"], IRI)
        self.assertIn("@context", doc)  # default context injected

    def test_missing_prefix_raises(self):
        with self.assertRaises(ValueError):
            normalize_authoring_payload({"slug": "x", "type": "Person", "name": "X"})

    def test_validate_create_rejects_unknown_type(self):
        with self.assertRaises((ValueError, JsonLdValidationError)):
            validate_create_payload(
                {"prefix": "person", "slug": "x", "type": "Wizard", "name": "X"}
            )
