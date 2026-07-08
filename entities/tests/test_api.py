"""DRF API + service tests for the NES JSON-LD read/write plane + bulk ingest.

Run under the platform settings (DB-less: sqlite fallback, managed
JSONB-backed tables) from the repo root:

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest entities/tests

Entities are stored as raw schema.org JSON-LD keyed by their ``@id`` IRI
(``https://jawafdehi.org/entity/<prefix>/<slug>``); every id in every response is
that IRI. The detail route accepts a url-encoded IRI OR the bare ``prefix/slug``.

Auth: the write planes use the shared OIDCAuthentication (Zitadel JWT -> Django
Groups). Authorized-path tests force-authenticate a user with the synced Group.
"""

from __future__ import annotations

from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from entities.services.bulk_ingest import BulkIngestService
from entities.services.publication import PublicationService

User = get_user_model()

IRI_BASE = "https://jawafdehi.org/entity"


class _DbAPITestCase(APITestCase):
    """APITestCase that may touch every database.

    Under the unified settings the DB router pins the
    ``entities`` models to the ``nes`` alias while auth.User lives in
    ``default``; Django's test runner only sets up ``default`` unless a test
    declares the databases it uses. ``"__all__"`` enrolls every alias so the
    routed ``nes`` queries are allowed in tests.
    """

    databases = "__all__"


def _person_payload(slug: str, full_name: str = "Ram Bahadur"):
    """The authoring shape the write API accepts (normalized to JSON-LD)."""
    return {
        "prefix": "person",
        "slug": slug,
        "type": "Person",
        "name": {"en": full_name},
        "change_description": "test create",
    }


def _person_iri(slug: str) -> str:
    return f"{IRI_BASE}/person/{slug}"


def _seed_entity(slug: str = "ram-bahadur"):
    """Create an entity directly via the publication service (round-trip seed)."""
    p = _person_payload(slug)
    from entities.write_validation import normalize_authoring_payload

    return PublicationService().create_entity(
        doc=normalize_authoring_payload(p),
        author_id="oidc:seed",
        change_description="seed",
    )


class ReadPlaneTests(_DbAPITestCase):
    def setUp(self):
        self.entity = _seed_entity()
        self.iri = self.entity["@id"]

    def test_health(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["service"], "nes-api")

    def test_list_entities_shape(self):
        resp = self.client.get("/api/entities")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for key in ("entities", "total", "limit", "offset"):
            self.assertIn(key, resp.data)
        self.assertEqual(resp.data["total"], 1)
        self.assertEqual(resp.data["entities"][0]["@id"], self.iri)

    def test_get_entity_detail_by_prefix_slug(self):
        resp = self.client.get("/api/entities/person/ram-bahadur")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["@id"], self.iri)
        self.assertEqual(resp.data["@type"], "Person")

    def test_get_entity_detail_by_encoded_iri(self):
        resp = self.client.get(f"/api/entities/{quote(self.iri, safe='')}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["@id"], self.iri)

    def test_get_entity_404(self):
        resp = self.client.get("/api/entities/person/nope")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_entity_prefixes_public(self):
        resp = self.client.get("/api/entity_prefixes")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("person", resp.data["prefixes"])

    def test_versions_endpoint(self):
        resp = self.client.get("/api/entities/person/ram-bahadur/versions")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["total"], 1)
        self.assertEqual(resp.data["versions"][0]["version_number"], 1)

    def test_tags_endpoint(self):
        resp = self.client.get("/api/entities/tags")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("tags", resp.data)

    def test_batch_lookup_by_iri(self):
        resp = self.client.get(f"/api/entities?ids={quote(self.iri, safe='')}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["total"], 1)
        self.assertEqual(resp.data["entities"][0]["@id"], self.iri)


class SearchAndCountTests(_DbAPITestCase):
    """Exercise the keyword/prefix filters, full-count totals, and clamping."""

    @classmethod
    def setUpTestData(cls):
        from entities.persistence import EntityRepository

        cls.repo = EntityRepository()
        for i in range(5):
            doc = _seed_entity(slug=f"person-{i}")
            doc["keywords"] = ["politician"] if i < 3 else ["bureaucrat"]
            cls.repo.put_entity(doc, version=1, created_at=_now())

    def test_total_reflects_full_filtered_set_not_page(self):
        resp = self.client.get("/api/entities?limit=2")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["entities"]), 2)
        self.assertEqual(resp.data["total"], 5)

    def test_keyword_filter_count_and_results(self):
        resp = self.client.get("/api/entities?keywords=politician")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["total"], 3)
        self.assertEqual(len(resp.data["entities"]), 3)

    def test_tags_alias_for_keywords(self):
        # ``tags`` is accepted as a back-compat alias for ``keywords``.
        resp = self.client.get("/api/entities?tags=bureaucrat")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["total"], 2)

    def test_limit_clamped_to_max(self):
        resp = self.client.get("/api/entities?limit=999999999")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertLessEqual(resp.data["limit"], 1000)

    def test_negative_offset_clamped(self):
        resp = self.client.get("/api/entities?offset=-5")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["offset"], 0)

    def test_query_search_matches(self):
        resp = self.client.get("/api/entities?query=Ram")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(resp.data["entities"]), 1)


class WritePlaneAuthTests(_DbAPITestCase):
    @classmethod
    def setUpTestData(cls):
        g, _ = Group.objects.get_or_create(name="Caseworker")
        cls.contributor = User.objects.create(username="oidc-sub-writer")
        cls.contributor.groups.add(g)
        cls.norole = User.objects.create(username="oidc-sub-norole")

    def test_unauth_create_is_401(self):
        resp = self.client.post("/api/entities", _person_payload("a-unauth"), format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_bogus_token_is_401(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-jwt")
        resp = self.client.post("/api/entities", _person_payload("b-bogus"), format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authed_without_role_is_403(self):
        self.client.force_authenticate(user=self.norole)
        resp = self.client.post("/api/entities", _person_payload("c-norole"), format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_moderator_can_write(self):
        moderator = User.objects.create(username="oidc-sub-moderator")
        g, _ = Group.objects.get_or_create(name="Moderator")
        moderator.groups.add(g)
        self.client.force_authenticate(user=moderator)
        resp = self.client.post("/api/entities", _person_payload("d-moderator"), format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, msg=resp.data)

    def test_readonly_cannot_write(self):
        readonly = User.objects.create(username="oidc-sub-readonly")
        g, _ = Group.objects.get_or_create(name="ReadOnly")
        readonly.groups.add(g)
        self.client.force_authenticate(user=readonly)
        resp = self.client.post("/api/entities", _person_payload("e-readonly"), format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_then_get_round_trip(self):
        self.client.force_authenticate(user=self.contributor)
        resp = self.client.post("/api/entities", _person_payload("created-via-api"), format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, msg=resp.data)
        iri = resp.data["@id"]
        self.assertEqual(iri, _person_iri("created-via-api"))
        self.assertEqual(resp.data["@type"], "Person")
        self.assertEqual(resp.data["jawafdehi:version"]["version_number"], 1)

        from rest_framework.test import APIClient

        got = APIClient().get("/api/entities/person/created-via-api")
        self.assertEqual(got.status_code, status.HTTP_200_OK)
        self.assertEqual(got.data["@id"], iri)

    def test_create_full_jsonld_doc(self):
        # A full JSON-LD doc (already carrying @id/@type) is accepted as-is.
        self.client.force_authenticate(user=self.contributor)
        doc = {
            "@type": "Person",
            "@id": _person_iri("full-jsonld"),
            "name": {"en": "Full JsonLd"},
        }
        resp = self.client.post("/api/entities", doc, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, msg=resp.data)
        self.assertEqual(resp.data["@id"], _person_iri("full-jsonld"))

    def test_create_invalid_type_is_422(self):
        self.client.force_authenticate(user=self.contributor)
        bad = {"prefix": "person", "slug": "bad-type", "type": "Wizard", "name": {"en": "X"}}
        resp = self.client.post("/api/entities", bad, format="json")
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

    def test_create_missing_name_is_422(self):
        self.client.force_authenticate(user=self.contributor)
        bad = {"prefix": "person", "slug": "no-name", "type": "Person"}
        resp = self.client.post("/api/entities", bad, format="json")
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

    def test_duplicate_create_is_409(self):
        self.client.force_authenticate(user=self.contributor)
        self.client.post("/api/entities", _person_payload("dup-slug"), format="json")
        resp = self.client.post("/api/entities", _person_payload("dup-slug"), format="json")
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)

    def test_patch_update_bumps_version(self):
        self.client.force_authenticate(user=self.contributor)
        self.client.post("/api/entities", _person_payload("patch-me"), format="json")
        patch = {
            "patch_ops": [{"op": "add", "path": "/keywords", "value": ["politician"]}],
            "change_description": "add keyword",
        }
        resp = self.client.patch("/api/entities/person/patch-me", patch, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, msg=resp.data)
        self.assertEqual(resp.data["jawafdehi:version"]["version_number"], 2)
        self.assertEqual(resp.data["keywords"], ["politician"])

    def test_patch_blocked_path_rejected(self):
        self.client.force_authenticate(user=self.contributor)
        self.client.post("/api/entities", _person_payload("blocked-patch"), format="json")
        for blocked in ("/@id", "/@type", "/@context", "/jawafdehi:version"):
            patch = {"patch_ops": [{"op": "replace", "path": blocked, "value": "x"}]}
            resp = self.client.patch("/api/entities/person/blocked-patch", patch, format="json")
            self.assertEqual(
                resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY, msg=blocked
            )


class BulkIngestServiceTests(_DbAPITestCase):
    """The ≥2-source HOLD gate: <2 distinct publishers -> HELD, >=2 -> created."""

    def test_two_sources_creates(self):
        records = [
            {
                "entity_prefix": "person",
                "entity_data": {
                    "slug": "two-source-person",
                    "type": "Person",
                    "name": {"en": "Two Source"},
                },
                "sources": [
                    {"url": "https://ecn.gov.np/x"},
                    {"url": "https://kathmandupost.com/y"},
                ],
            }
        ]
        result = BulkIngestService().ingest_entities(records, author_id="author:test")
        self.assertEqual(result.created, 1)
        self.assertEqual(result.held, 0)
        got = self.client.get("/api/entities/person/two-source-person")
        self.assertEqual(got.status_code, status.HTTP_200_OK)

    def test_single_source_is_held(self):
        records = [
            {
                "entity_prefix": "person",
                "entity_data": {
                    "slug": "held-person",
                    "type": "Person",
                    "name": {"en": "Held"},
                },
                "sources": [{"url": "https://ecn.gov.np/only"}],
            }
        ]
        result = BulkIngestService().ingest_entities(records, author_id="author:test")
        self.assertEqual(result.created, 0)
        self.assertEqual(result.held, 1)
        self.assertIn(_person_iri("held-person"), result.held_ids)
        got = self.client.get("/api/entities/person/held-person")
        self.assertEqual(got.status_code, status.HTTP_404_NOT_FOUND)

    def test_same_publisher_does_not_corroborate(self):
        records = [
            {
                "entity_prefix": "person",
                "entity_data": {
                    "slug": "same-pub",
                    "type": "Person",
                    "name": {"en": "Same Pub"},
                },
                "sources": [
                    {"url": "https://results.ecn.gov.np/a"},
                    {"url": "https://data.ecn.gov.np/b"},
                ],
            }
        ]
        result = BulkIngestService().ingest_entities(records, author_id="author:test")
        self.assertEqual(result.held, 1)
        self.assertEqual(result.created, 0)


class CanonicalAuthorityStoreTests(_DbAPITestCase):
    """The scheme+host is part of the join key, so it MUST be canonicalized on
    store — a foreign-host @id is re-keyed onto ``iri_base()`` before persist,
    and the stored PK + lookups all share one authority."""

    def test_create_canonicalizes_foreign_host_id(self):
        from entities.persistence import EntityRepository

        doc = {
            "@context": "https://schema.org",
            "@type": "Person",
            # foreign host + non-canonical scheme/port
            "@id": "http://evil.com:8443/entity/person/canon-me",
            "name": {"en": "Canon Me"},
        }
        created = PublicationService().create_entity(
            doc=doc, author_id="oidc:seed", change_description="seed"
        )
        canonical = "https://jawafdehi.org/entity/person/canon-me"
        # The returned + stored @id is the canonical authority, not evil.com.
        self.assertEqual(created["@id"], canonical)
        self.assertIsNotNone(EntityRepository().get_entity(canonical))
        # The non-canonical host was never persisted as a separate PK.
        self.assertIsNone(EntityRepository().get_entity(doc_id_was := "http://evil.com:8443/entity/person/canon-me"))
        self.assertNotEqual(doc_id_was, canonical)

    def test_persistence_put_entity_canonicalizes(self):
        from entities.persistence import EntityRepository

        repo = EntityRepository()
        doc = {
            "@type": "Person",
            "@id": "https://x:8443/entity/person/direct-put",
            "name": {"en": "Direct Put"},
        }
        repo.put_entity(doc, version=1, created_at=_now())
        canonical = "https://jawafdehi.org/entity/person/direct-put"
        self.assertEqual(doc["@id"], canonical)  # mutated in place
        self.assertIsNotNone(repo.get_entity(canonical))

    def test_lookup_resolves_foreign_host_to_canonical(self):
        from urllib.parse import quote

        _seed_entity("lookup-canon")
        foreign = "http://evil.com/entity/person/lookup-canon"
        resp = self.client.get(f"/api/entities/{quote(foreign, safe='')}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            resp.data["@id"], "https://jawafdehi.org/entity/person/lookup-canon"
        )


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


class AdminPlaneAuthTests(_DbAPITestCase):
    """Reindex (admin plane) is gated on Moderator/Admin — NOT the write role."""

    @classmethod
    def setUpTestData(cls):
        cls.caseworker = User.objects.create(username="oidc-sub-cw")
        cls.caseworker.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
        cls.moderator = User.objects.create(username="oidc-sub-mod")
        cls.moderator.groups.add(Group.objects.get_or_create(name="Moderator")[0])

    def test_reindex_unauth_is_401(self):
        resp = self.client.post("/api/admin/reindex")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_reindex_write_role_is_403(self):
        # A content/write role (Caseworker) must NOT reach the admin plane.
        self.client.force_authenticate(user=self.caseworker)
        resp = self.client.post("/api/admin/reindex")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_reindex_moderator_ok(self):
        self.client.force_authenticate(user=self.moderator)
        resp = self.client.post("/api/admin/reindex")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
