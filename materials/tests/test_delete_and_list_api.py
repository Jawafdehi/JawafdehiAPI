"""DRF API tests for the materials LIST endpoint + SOFT-DELETE plane.

- ``GET /api/materials/`` (no ``iri``) returns a paginated list of live
  materials in the platform ``{results, next}`` shape (PlatformCursorPagination),
  while ``GET /api/materials/?iri=`` keeps its single-lookup behavior.
- ``DELETE /api/materials/<source>/<ident>`` and
  ``DELETE /api/materials/?iri=`` soft-delete the stored material
  (``is_deleted=True``, never hard-delete), returning 204; deleted materials
  disappear from list + detail. Writes are NGM-role gated.

Run under the platform settings (DB-less: sqlite fallback) from the repo root::

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_delete_and_list_api.py
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from materials.jsonld import MATERIAL_CONTEXT
from materials.models import Material

User = get_user_model()


def _doc(iri: str):
    return {
        "@context": MATERIAL_CONTEXT,
        "@type": "Legislation",
        "@id": iri,
        "name": {"ne": "ऐन"},
    }


def _seed_material(source: str, ident: str) -> str:
    iri = f"https://jawafdehi.org/material/{source}/{ident}"
    Material.objects.create(
        iri=iri, material_type="legal_corpus", source=source, ident=ident,
        data=_doc(iri),
    )
    return iri


class MaterialsListTests(APITestCase):
    databases = "__all__"

    def test_list_empty_shape(self):
        resp = self.client.get("/api/materials/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Platform cursor pagination -> {results, next}
        self.assertIn("results", resp.data)
        self.assertIn("next", resp.data)
        self.assertEqual(resp.data["results"], [])

    def test_list_returns_live_materials(self):
        iri = _seed_material("nkp", "2080-act-1")
        resp = self.client.get("/api/materials/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["results"]), 1)
        self.assertEqual(resp.data["results"][0]["@id"], iri)

    def test_list_filter_by_source(self):
        _seed_material("nkp", "2080-act-1")
        _seed_material("ppmo", "report-9")
        resp = self.client.get("/api/materials/", {"source": "ppmo"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["results"]), 1)
        self.assertTrue(resp.data["results"][0]["@id"].endswith("ppmo/report-9"))

    def test_single_lookup_by_iri_still_works(self):
        iri = _seed_material("nkp", "2080-act-1")
        resp = self.client.get("/api/materials/", {"iri": iri})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["@id"], iri)
        self.assertEqual(resp.data["@type"], "Legislation")

    def test_list_excludes_soft_deleted(self):
        _seed_material("nkp", "2080-act-1")
        Material.objects.filter(source="nkp").update(is_deleted=True)
        resp = self.client.get("/api/materials/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["results"], [])


class MaterialsDeleteTests(APITestCase):
    databases = "__all__"

    @classmethod
    def setUpTestData(cls):
        g, _ = Group.objects.get_or_create(name="Caseworker")  # NGM role
        cls.user = User.objects.create(username="oidc-ngm-writer")
        cls.user.groups.add(g)
        cls.norole = User.objects.create(username="oidc-ngm-norole")

    def setUp(self):
        self.iri = _seed_material("nkp", "2080-act-1")

    def test_delete_by_path_soft_deletes_204(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.delete("/api/materials/nkp/2080-act-1")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        row = Material.objects.get(pk=self.iri)
        self.assertTrue(row.is_deleted)

    def test_delete_by_iri_query_soft_deletes_204(self):
        from urllib.parse import quote

        self.client.force_authenticate(user=self.user)
        # iri travels in the query string (DELETE has no body contract here).
        resp = self.client.delete(f"/api/materials/?iri={quote(self.iri, safe='')}")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertTrue(Material.objects.get(pk=self.iri).is_deleted)

    def test_deleted_hidden_from_detail(self):
        self.client.force_authenticate(user=self.user)
        self.client.delete("/api/materials/nkp/2080-act-1")
        self.client.force_authenticate(user=None)
        resp = self.client.get("/api/materials/nkp/2080-act-1")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_unknown_is_404(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.delete("/api/materials/nkp/does-not-exist")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_unauth_is_401(self):
        resp = self.client.delete("/api/materials/nkp/2080-act-1")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(Material.objects.get(pk=self.iri).is_deleted)

    def test_delete_authed_without_role_is_403(self):
        self.client.force_authenticate(user=self.norole)
        resp = self.client.delete("/api/materials/nkp/2080-act-1")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Material.objects.get(pk=self.iri).is_deleted)

    def test_reupsert_revives_soft_deleted(self):
        self.client.force_authenticate(user=self.user)
        self.client.delete("/api/materials/nkp/2080-act-1")
        resp = self.client.put(
            "/api/materials/nkp/2080-act-1", _doc(self.iri), format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertFalse(Material.objects.get(pk=self.iri).is_deleted)
