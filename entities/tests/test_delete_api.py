"""DRF API tests for the NES entity SOFT-DELETE plane.

``DELETE /api/entities/{ref}`` flips ``is_deleted=True`` (never hard-deletes —
this is an accountability/audit platform): the entity vanishes from list /
detail / search but the row (and version history) survive. Auth mirrors the
write plane (``HasNesContributorRole`` -> the ``NES_Contributor`` Django Group);
unauthenticated / authenticated-without-role assert the 401 / 403 contract.

Run under the platform settings (DB-less: sqlite fallback) from the repo root::

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest entities/tests/test_delete_api.py
"""

from __future__ import annotations

from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from entities.models import StoredEntity
from entities.services.publication import PublicationService
from entities.write_validation import normalize_authoring_payload

User = get_user_model()

IRI_BASE = "https://jawafdehi.org/entity"


def _person_payload(slug: str, full_name: str = "Ram Bahadur"):
    return {
        "prefix": "person",
        "slug": slug,
        "type": "Person",
        "name": {"en": full_name},
        "change_description": "test create",
    }


def _seed_entity(slug: str = "ram-bahadur"):
    return PublicationService().create_entity(
        doc=normalize_authoring_payload(_person_payload(slug)),
        author_id="oidc:seed",
        change_description="seed",
    )


class EntityDeleteTests(APITestCase):
    databases = "__all__"

    @classmethod
    def setUpTestData(cls):
        g, _ = Group.objects.get_or_create(name="NES_Contributor")
        cls.contributor = User.objects.create(username="oidc-sub-writer")
        cls.contributor.groups.add(g)
        cls.norole = User.objects.create(username="oidc-sub-norole")

    def setUp(self):
        self.entity = _seed_entity()
        self.iri = self.entity["@id"]

    def test_delete_soft_deletes_and_returns_204(self):
        self.client.force_authenticate(user=self.contributor)
        resp = self.client.delete("/api/entities/person/ram-bahadur")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        # Row still exists (never hard-deleted); flagged soft-deleted.
        row = StoredEntity.objects.get(pk=self.iri)
        self.assertTrue(row.is_deleted)

    def test_deleted_entity_hidden_from_detail(self):
        self.client.force_authenticate(user=self.contributor)
        self.client.delete("/api/entities/person/ram-bahadur")
        self.client.force_authenticate(user=None)
        resp = self.client.get("/api/entities/person/ram-bahadur")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_deleted_entity_hidden_from_list(self):
        self.client.force_authenticate(user=self.contributor)
        self.client.delete("/api/entities/person/ram-bahadur")
        self.client.force_authenticate(user=None)
        resp = self.client.get("/api/entities")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["total"], 0)
        self.assertEqual(resp.data["entities"], [])

    def test_delete_unknown_is_404(self):
        self.client.force_authenticate(user=self.contributor)
        resp = self.client.delete("/api/entities/person/no-such-slug")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_double_delete_is_404(self):
        self.client.force_authenticate(user=self.contributor)
        first = self.client.delete("/api/entities/person/ram-bahadur")
        self.assertEqual(first.status_code, status.HTTP_204_NO_CONTENT)
        second = self.client.delete("/api/entities/person/ram-bahadur")
        self.assertEqual(second.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_unauth_is_401(self):
        resp = self.client.delete("/api/entities/person/ram-bahadur")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(StoredEntity.objects.get(pk=self.iri).is_deleted)

    def test_delete_authed_without_role_is_403(self):
        self.client.force_authenticate(user=self.norole)
        resp = self.client.delete("/api/entities/person/ram-bahadur")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(StoredEntity.objects.get(pk=self.iri).is_deleted)

    def test_delete_by_encoded_iri(self):
        self.client.force_authenticate(user=self.contributor)
        resp = self.client.delete(f"/api/entities/{quote(self.iri, safe='')}")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(StoredEntity.objects.get(pk=self.iri).is_deleted)

    def test_recreate_after_delete_revives(self):
        # A soft-deleted IRI can be re-created; the upsert clears is_deleted.
        self.client.force_authenticate(user=self.contributor)
        self.client.delete("/api/entities/person/ram-bahadur")
        resp = self.client.post(
            "/api/entities", _person_payload("ram-bahadur"), format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertFalse(StoredEntity.objects.get(pk=self.iri).is_deleted)
