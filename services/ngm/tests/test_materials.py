"""Tests for the schema.org JSON-LD material plane (PART B) + IRI re-key (PART A).

Run under the monolith settings (DB-less: sqlite fallback, managed tables)
from the repo root:

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest services/ngm/tests

Covers:
- nes_id entity-IRI validation on CaseEntity / BlacklistedFirm / CourtCase,
- material IRI + JSON-LD @type/@id/@context shape (court-case record),
- document_sources → associatedMedia / MediaObject (roled links),
- the GET /api/materials/{iri} read endpoint (stored + derived court case),
- the ingestion resolve IRI gate.
"""

from __future__ import annotations

from datetime import date

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from jawafdehi_shared.entities.ids import is_valid_material_iri
from ngm_service.courts.models import (
    BlacklistedFirm,
    CaseEntity,
    Court,
    CourtCase,
    validate_entity_iri,
)
from ngm_service.materials.jsonld import (
    MATERIAL_CONTEXT,
    MaterialType,
    court_case_material_iri,
    court_case_to_jsonld,
    index_node_jsonld,
    manuscript_jsonld,
    media_objects_from_document_sources,
    validate_material_jsonld,
)
from ngm_service.materials.models import Material

User = get_user_model()


class _DbAPITestCase(APITestCase):
    """APITestCase that may touch every database.

    Under the consolidated (monolith) settings the DB router pins the NGM
    ``courts``/``materials`` models to the ``ngm`` alias while auth.User lives in
    ``default``; Django's test runner only sets up ``default`` unless a test
    declares the databases it uses. ``"__all__"`` enrolls every alias so the
    routed ``ngm`` queries are allowed in tests.
    """

    databases = "__all__"


# ── PART A: entity nes_id IRI validation ─────────────────────────────────────


class EntityIriValidationTests(_DbAPITestCase):
    def test_validator_accepts_canonical_iri(self):
        validate_entity_iri("https://jawafdehi.org/entity/person/ram-bahadur")  # no raise

    def test_validator_allows_blank(self):
        validate_entity_iri("")
        validate_entity_iri(None)

    def test_validator_rejects_legacy_opaque_form(self):
        with self.assertRaises(ValidationError):
            validate_entity_iri("entity:person/ram-bahadur")

    def test_validator_rejects_garbage(self):
        with self.assertRaises(ValidationError):
            validate_entity_iri("not-an-iri")

    def test_validator_strict_rejects_noncanonical_host(self):
        # The host is part of the join key: a valid-shaped IRI on a foreign host,
        # wrong scheme, or a port must be rejected at this write boundary.
        for bad in (
            "http://evil.com/entity/person/ram-bahadur",
            "https://x:8443/entity/person/ram-bahadur",
            "http://jawafdehi.org/entity/person/ram-bahadur",  # wrong scheme
        ):
            with self.assertRaises(ValidationError):
                validate_entity_iri(bad)

    def test_validator_rejects_over_max_length(self):
        too_long = "https://jawafdehi.org/entity/person/" + ("a" * 300)
        with self.assertRaises(ValidationError):
            validate_entity_iri(too_long)

    def test_model_full_clean_enforces_iri(self):
        court = Court.objects.create(
            identifier="supreme", court_type="supreme", full_name_nepali="स"
        )
        ent = CaseEntity(
            case_number="082-OA-0503", court=court, side="plaintiff",
            name="Ram", nes_id="entity:person/ram-bahadur",
        )
        with self.assertRaises(ValidationError):
            ent.full_clean()
        ent.nes_id = "https://jawafdehi.org/entity/person/ram-bahadur"
        ent.full_clean()  # no raise

    def test_firm_field_widened_to_300(self):
        self.assertEqual(BlacklistedFirm._meta.get_field("nes_id").max_length, 300)
        self.assertEqual(CaseEntity._meta.get_field("nes_id").max_length, 300)
        self.assertEqual(CourtCase._meta.get_field("nes_id").max_length, 300)


# ── PART B: court-case material JSON-LD shape ────────────────────────────────


class CourtCaseJsonLdTests(_DbAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.court = Court.objects.create(
            identifier="kathmandudc", court_type="district", full_name_nepali="ज"
        )
        cls.case = CourtCase.objects.create(
            case_number="082-OA-0503", court=cls.court,
            case_type="भ्रष्टाचार", case_status="चालु",
            registration_date_ad=date(2026, 1, 11),
            registration_date_bs="2082-09-28",
            plaintiff="राम", defendant="श्याम",
            nes_id="https://jawafdehi.org/entity/org/ciaa",
            document_sources=[
                {
                    "document_id": "ngm:court-order:kathmandudc:082-OA-0503",
                    "url": [
                        {"link": "https://r2/raw.pdf", "role": "RAW"},
                        {"link": "https://r2/alt.docx", "role": "ALTERNATE"},
                        {"link": "https://perma/x", "role": "PERMALINK"},
                    ],
                }
            ],
        )
        CaseEntity.objects.create(
            case_number="082-OA-0503", court=cls.court, side="plaintiff",
            name="Ram", nes_id="https://jawafdehi.org/entity/person/ram",
        )

    def test_material_iri_is_canonical(self):
        iri = court_case_material_iri("kathmandudc", "082-OA-0503")
        self.assertTrue(is_valid_material_iri(iri))
        self.assertEqual(
            iri, "https://jawafdehi.org/material/court/kathmandudc.082-oa-0503"
        )

    def test_jsonld_shape(self):
        doc = court_case_to_jsonld(self.case)
        self.assertEqual(doc["@context"], MATERIAL_CONTEXT)
        self.assertEqual(doc["@type"], "CreativeWork")
        self.assertEqual(doc["additionalType"], "jawafdehi:CourtCase")
        self.assertTrue(is_valid_material_iri(doc["@id"]))
        self.assertIn("name", doc)
        self.assertEqual(doc["jawafdehi:caseType"], "भ्रष्टाचार")
        self.assertEqual(doc["dateCreated"], "2026-01-11")
        # about: case-level + party entity IRIs.
        about_ids = {a["@id"] for a in doc["about"]}
        self.assertIn("https://jawafdehi.org/entity/org/ciaa", about_ids)
        self.assertIn("https://jawafdehi.org/entity/person/ram", about_ids)
        # LOCKED #1: orders are referenced as standalone court_order Materials
        # via hasPart (NOT embedded associatedMedia). This case has one order.
        self.assertNotIn("associatedMedia", doc)
        self.assertEqual(len(doc["hasPart"]), 1)
        self.assertEqual(
            doc["hasPart"][0]["@id"],
            "https://jawafdehi.org/material/court_order/kathmandudc.082-oa-0503",
        )

    def test_jsonld_passes_validator(self):
        validate_material_jsonld(court_case_to_jsonld(self.case))


class DocumentSourcesMediaObjectTests(_DbAPITestCase):
    def test_roled_links_become_media_objects(self):
        media = media_objects_from_document_sources(
            [
                {
                    "document_id": "ngm:doc:1",
                    "url": [
                        {"link": "https://r2/raw.pdf", "role": "RAW"},
                        {"link": "https://r2/alt.docx", "role": "ALTERNATE"},
                        {"link": "https://perma/x", "role": "PERMALINK"},
                        {"link": "https://gov/page", "role": "SOURCE_PAGE"},
                    ],
                }
            ]
        )
        self.assertEqual(len(media), 4)
        self.assertTrue(all(m["@type"] == "MediaObject" for m in media))
        self.assertEqual(media[0]["contentUrl"], "https://r2/raw.pdf")
        self.assertEqual(media[0]["jawafdehi:linkRole"], "RAW")
        # SOURCE_PAGE gets an encodingFormat hint.
        self.assertEqual(media[3]["encodingFormat"], "text/html")
        # document_id rides as identifier.
        self.assertEqual(media[0]["identifier"], "ngm:doc:1")

    def test_empty_and_malformed_are_skipped(self):
        self.assertEqual(media_objects_from_document_sources(None), [])
        self.assertEqual(media_objects_from_document_sources([{"url": "nope"}]), [])
        self.assertEqual(
            media_objects_from_document_sources([{"url": [{"link": ""}]}]), []
        )


class MaterialModelTests(_DbAPITestCase):
    def test_from_jsonld_derives_columns_and_validates(self):
        doc = {
            "@context": MATERIAL_CONTEXT,
            "@type": "Legislation",
            "@id": "https://jawafdehi.org/material/nkp/2080-act-1",
            "name": {"ne": "ऐन"},
        }
        m = Material.from_jsonld(doc, material_type=MaterialType.LEGAL_CORPUS)
        self.assertEqual(m.source, "nkp")
        self.assertEqual(m.ident, "2080-act-1")
        m.full_clean()  # clean() re-validates iri/columns/jsonld
        m.save()
        self.assertEqual(Material.objects.get(pk=doc["@id"]).material_type, "legal_corpus")

    def test_validator_rejects_unknown_type(self):
        with self.assertRaises(ValueError):
            validate_material_jsonld(
                {"@type": "Banana", "@id": "https://jawafdehi.org/material/x/y", "name": "n"}
            )

    def test_validator_rejects_bad_iri(self):
        with self.assertRaises(ValueError):
            validate_material_jsonld(
                {"@type": "Report", "@id": "https://jawafdehi.org/entity/x/y", "name": "n"}
            )

    def test_validator_requires_name(self):
        with self.assertRaises(ValueError):
            validate_material_jsonld(
                {"@type": "Report", "@id": "https://jawafdehi.org/material/x/y"}
            )

    def test_validator_strict_rejects_noncanonical_host(self):
        # Material @id host is part of the join key — reject foreign host/scheme.
        for bad in (
            "http://evil.com/material/court/sc.123",
            "https://x:8443/material/court/sc.123",
        ):
            with self.assertRaises(ValueError):
                validate_material_jsonld({"@type": "Report", "@id": bad, "name": "n"})

    def test_model_clean_rejects_noncanonical_host(self):
        from django.core.exceptions import ValidationError as DjValidationError

        m = Material(
            iri="http://evil.com/material/court/sc.123",
            material_type=MaterialType.LEGAL_CORPUS,
            source="court", ident="sc.123",
            data={"@type": "Report", "@id": "http://evil.com/material/court/sc.123", "name": "n"},
        )
        with self.assertRaises(DjValidationError):
            m.clean()

    def test_material_iri_field_aligned_to_max_length(self):
        from jawafdehi_shared.entities.ids import MAX_IRI_LENGTH

        self.assertEqual(Material._meta.get_field("iri").max_length, MAX_IRI_LENGTH)


class MaterialEndpointTests(_DbAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.court = Court.objects.create(
            identifier="kathmandudc", court_type="district", full_name_nepali="ज"
        )
        cls.case = CourtCase.objects.create(
            case_number="082-OA-0503", court=cls.court, case_status="चालु",
            document_sources=[
                {"document_id": "d1", "url": [{"link": "https://r2/raw.pdf", "role": "RAW"}]}
            ],
        )
        # A stored material (legal corpus).
        cls.material = Material.objects.create(
            iri="https://jawafdehi.org/material/nkp/2080-act-1",
            material_type=MaterialType.LEGAL_CORPUS,
            source="nkp", ident="2080-act-1",
            data={
                "@context": MATERIAL_CONTEXT, "@type": "Legislation",
                "@id": "https://jawafdehi.org/material/nkp/2080-act-1",
                "name": {"ne": "ऐन"},
            },
        )

    def test_get_stored_material_by_path(self):
        resp = self.client.get("/api/ngm/materials/nkp/2080-act-1")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["@type"], "Legislation")

    def test_get_stored_material_by_iri_query(self):
        resp = self.client.get(
            "/api/ngm/materials/", {"iri": "https://jawafdehi.org/material/nkp/2080-act-1"}
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["@id"], self.material.iri)

    def test_get_derived_court_case_material(self):
        # No stored row; materialized on the fly from the court tables.
        iri = court_case_material_iri("kathmandudc", "082-OA-0503")
        resp = self.client.get("/api/ngm/materials/", {"iri": iri})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["additionalType"], "jawafdehi:CourtCase")
        # LOCKED #1: one order referenced via hasPart, not embedded media.
        self.assertEqual(len(resp.data["hasPart"]), 1)

    def test_missing_material_is_404(self):
        resp = self.client.get("/api/ngm/materials/nkp/does-not-exist")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_iri_query_required(self):
        resp = self.client.get("/api/ngm/materials/")
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

    def test_case_detail_exposes_material_id(self):
        resp = self.client.get("/api/ngm/cases/kathmandudc/082-OA-0503")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(is_valid_material_iri(resp.data["material_id"]))


# ── PART C: R2 index JSON-LD node shape ──────────────────────────────────────


class IndexJsonLdNodeShapeTests(_DbAPITestCase):
    def test_manuscript_node_shape(self):
        ms = {
            "url": "https://ngm-store/raw.pdf",
            "file_name": "082-OA-0503.pdf",
            "metadata": {"title": "आदेश", "publication_date": "2026-01-11"},
            "links": [
                {"link": "https://ngm-store/raw.pdf", "role": "RAW"},
                {"link": "https://ngm-store/alt.docx", "role": "ALTERNATE"},
            ],
            "document_id": "ngm:court-order:supreme:082-OA-0503",
            "source_type": "COURT_ORDER",
        }
        doc = manuscript_jsonld(ms)
        self.assertEqual(doc["@context"], MATERIAL_CONTEXT)
        self.assertEqual(doc["@type"], ["Manuscript", "DigitalDocument"])
        self.assertTrue(is_valid_material_iri(doc["@id"]))
        self.assertEqual(doc["identifier"], "ngm:court-order:supreme:082-OA-0503")
        self.assertEqual(doc["datePublished"], "2026-01-11")
        self.assertEqual(len(doc["associatedMedia"]), 2)
        self.assertEqual(doc["associatedMedia"][0]["jawafdehi:linkRole"], "RAW")

    def test_press_release_maps_to_charge_sheet(self):
        doc = manuscript_jsonld(
            {"document_id": "ngm:ciaa-press-release:42", "source_type": "CIAA_PRESS_RELEASE",
             "links": [], "metadata": {}}
        )
        self.assertEqual(doc["@type"], "DigitalDocument")
        self.assertEqual(doc["additionalType"], "jawafdehi:ChargeSheet")

    def test_leaf_node_shape(self):
        node = {
            "name": "082-OA-0503",
            "path": "/court-orders/supreme/082/082-OA-0503",
            "manuscripts": [
                {"document_id": "ngm:court-order:supreme:082-OA-0503",
                 "source_type": "COURT_ORDER", "links": [
                     {"link": "https://ngm-store/raw.pdf", "role": "RAW"}], "metadata": {}}
            ],
        }
        doc = index_node_jsonld(node)
        self.assertEqual(doc["@type"], "Collection")
        self.assertEqual(doc["@id"], "https://jawafdehi.org/index/court-orders/supreme/082/082-OA-0503")
        self.assertEqual(len(doc["hasPart"]), 1)
        self.assertEqual(doc["hasPart"][0]["@type"], ["Manuscript", "DigitalDocument"])

    def test_branch_node_shape_with_refs_and_next(self):
        node = {
            "name": "court-orders",
            "path": "/court-orders",
            "children": [
                {"name": "supreme", "path": "/court-orders/supreme",
                 "$ref": "https://ngm-store/indices/2026-06-28/index.court-orders.supreme.json"},
            ],
            "next": "https://ngm-store/indices/2026-06-28/index.court-orders.page-2.json",
        }
        doc = index_node_jsonld(node)
        self.assertEqual(doc["@type"], "CollectionPage")
        self.assertEqual(len(doc["hasPart"]), 1)
        self.assertEqual(doc["hasPart"][0]["url"], node["children"][0]["$ref"])
        self.assertEqual(doc["jawafdehi:next"], node["next"])

    def test_lakehouse_index_publish_seam(self):
        from ngm_service.lakehouse import index_publish

        # The shaping seam re-exports the pure functions...
        self.assertIs(index_publish.index_node_jsonld, index_node_jsonld)
        # ...and node_tree_to_jsonld shapes a nested tree end-to-end.
        tree = {
            "name": "root", "path": "/",
            "children": [
                {"name": "court-orders", "path": "/court-orders", "children": [
                    {"name": "supreme", "path": "/court-orders/supreme", "manuscripts": [
                        {"document_id": "ngm:court-order:supreme:1",
                         "source_type": "COURT_ORDER", "links": [
                             {"link": "https://r2/x.pdf", "role": "RAW"}], "metadata": {}}
                    ]},
                ]},
            ],
        }
        shaped = index_publish.node_tree_to_jsonld(tree)
        self.assertEqual(shaped["@type"], "CollectionPage")
        self.assertTrue(shaped["hasPart"])

    def test_node_object_key_mirrors_index_path(self):
        from ngm_service.lakehouse import index_publish

        self.assertEqual(
            index_publish.node_object_key({"jawafdehi:indexPath": "/"}),
            "index/index.jsonld",
        )
        self.assertEqual(
            index_publish.node_object_key(
                {"jawafdehi:indexPath": "/court-orders/supreme"}
            ),
            "index/court-orders/supreme.jsonld",
        )

    def test_publish_index_jsonld_writes_one_object_per_node(self):
        import json

        from ngm_service.lakehouse import index_publish

        # A stub S3 client capturing put_object calls (boto3-compatible signature).
        class _StubS3:
            def __init__(self):
                self.puts = []

            def put_object(self, *, Bucket, Key, Body, ContentType):
                self.puts.append(
                    {"Bucket": Bucket, "Key": Key, "Body": Body, "ContentType": ContentType}
                )

        tree = {
            "name": "root", "path": "/",
            "children": [
                {"name": "court-orders", "path": "/court-orders", "children": [
                    {"name": "supreme", "path": "/court-orders/supreme", "manuscripts": [
                        {"document_id": "ngm:court-order:supreme:1",
                         "source_type": "COURT_ORDER", "links": [
                             {"link": "https://r2/x.pdf", "role": "RAW"}], "metadata": {}}
                    ]},
                ]},
            ],
        }
        stub = _StubS3()
        written = index_publish.publish_index_jsonld(
            [tree], client=stub, bucket="ngm-gold"
        )
        # root + court-orders + supreme = 3 path-bearing nodes written.
        self.assertEqual(written, 3)
        self.assertEqual(len(stub.puts), 3)
        keys = {p["Key"] for p in stub.puts}
        self.assertIn("index/index.jsonld", keys)
        self.assertIn("index/court-orders.jsonld", keys)
        self.assertIn("index/court-orders/supreme.jsonld", keys)
        # Every object is valid JSON-LD with the linked-data content type.
        for put in stub.puts:
            self.assertEqual(put["ContentType"], "application/ld+json")
            doc = json.loads(put["Body"].decode("utf-8"))
            self.assertIn("@context", doc)
            self.assertIn("@type", doc)

    def test_publish_index_jsonld_skips_ref_child_stubs(self):
        """A `$ref` child (a link stub, full content lives in its own file) must
        NOT be written as a separate object — it carries an indexPath but no @id,
        so writing it would clobber the real node at the same key."""
        from ngm_service.lakehouse import index_publish

        class _StubS3:
            def __init__(self):
                self.keys = []

            def put_object(self, *, Bucket, Key, Body, ContentType):
                self.keys.append(Key)

        # Branch with a $ref child (not inlined) → the child is a link stub only.
        tree = {
            "name": "root", "path": "/",
            "children": [
                {"name": "court-orders", "path": "/court-orders",
                 "$ref": "/court-orders.jsonld"},
            ],
        }
        stub = _StubS3()
        written = index_publish.publish_index_jsonld([tree], client=stub, bucket="g")
        # Only the root is written; the $ref child stub is skipped.
        self.assertEqual(written, 1)
        self.assertEqual(stub.keys, ["index/index.jsonld"])
        self.assertNotIn("index/court-orders.jsonld", stub.keys)

    def test_publish_index_jsonld_requires_a_bucket(self):
        from ngm_service.lakehouse import index_publish

        class _StubS3:
            def put_object(self, **_kwargs):  # pragma: no cover - never reached
                raise AssertionError("should not write without a bucket")

        with self.assertRaises(RuntimeError):
            index_publish.publish_index_jsonld([{"name": "x", "path": "/"}], client=_StubS3(), bucket="")

    def test_publish_index_jsonld_requires_client_or_credentials(self):
        from ngm_service.lakehouse import config, index_publish

        # Unconfigured object store + no injected client → clear RuntimeError.
        empty = config.load_settings()
        if empty.s3.is_configured:
            self.skipTest("object store is configured in this environment")
        with self.assertRaises(RuntimeError):
            index_publish.publish_index_jsonld(
                [{"name": "x", "path": "/"}], settings=empty, bucket="ngm-gold"
            )
