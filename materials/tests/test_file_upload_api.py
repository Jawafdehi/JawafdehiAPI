"""API tests for material FILE UPLOAD (``POST /api/materials/<source>/<ident>/file``).

Multipart upload streams a file to storage (FileSystemStorage under tests) and
appends a schema.org ``MediaObject`` to the material's ``associatedMedia`` —
creating the material when it does not yet exist. NGM-role gated (mirrors
``courts/tests/test_write_api.py`` auth setup).

Run (DB-less: sqlite fallback, managed materials table) from the repo root::

    SECRET_KEY=dev ALLOWED_HOSTS='*' uv run pytest materials/tests/test_file_upload_api.py
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase

from materials.models import Material

User = get_user_model()


class _DbAPITestCase(APITestCase):
    databases = "__all__"

    @classmethod
    def _make_caseworker(cls):
        g, _ = Group.objects.get_or_create(name="Caseworker")
        u = User.objects.create(username="oidc-writer")
        u.groups.add(g)
        return u

    @classmethod
    def _make_norole(cls):
        return User.objects.create(username="oidc-norole")


class MaterialFileUploadTests(_DbAPITestCase):
    URL = "/api/materials/nkp/2080-order-1/file"
    IRI = "https://jawafdehi.org/material/nkp/2080-order-1"

    @classmethod
    def setUpTestData(cls):
        cls.user = cls._make_caseworker()
        cls.norole = cls._make_norole()

    def _pdf(self, name="order.pdf"):
        return SimpleUploadedFile(name, b"%PDF-1.4 data", content_type="application/pdf")

    def test_upload_creates_material_with_media_object(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            self.URL,
            {"file": self._pdf(), "material_type": "court_order"},
            format="multipart",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["@id"], self.IRI)
        media = resp.data["associatedMedia"]
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["@type"], "MediaObject")
        self.assertEqual(media[0]["jawafdehi:linkRole"], "RAW")
        self.assertTrue(media[0]["contentUrl"].endswith(".pdf"))
        self.assertEqual(media[0]["encodingFormat"], "application/pdf")

        row = Material.objects.get(pk=self.IRI)
        self.assertEqual(row.material_type, "court_order")

    def test_upload_records_provenance_on_media_object(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            self.URL,
            {"file": self._pdf(), "material_type": "court_order",
             "source_url": "https://supremecourt.gov.np/x.pdf"},
            format="multipart",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        prov = resp.data["associatedMedia"][0]["jawafdehi:provenance"]
        self.assertEqual(prov["fetch_method"], "upload")
        self.assertEqual(prov["source_url"], "https://supremecourt.gov.np/x.pdf")
        self.assertEqual(len(prov["sha256"]), 64)  # sha256 hex
        self.assertIn("captured_at", prov)
        self.assertEqual(prov["content_length"], len(b"%PDF-1.4 data"))

    def test_reupload_identical_bytes_same_role_dedups(self):
        self.client.force_authenticate(user=self.user)
        self.client.post(
            self.URL,
            {"file": self._pdf(), "material_type": "court_order"},
            format="multipart",
        )
        # Re-upload the SAME bytes at the SAME role → content-hash idempotency:
        # the MediaObject is replaced, not duplicated.
        resp = self.client.post(
            self.URL, {"file": self._pdf()}, format="multipart"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        raw_media = [
            m for m in resp.data["associatedMedia"]
            if m["jawafdehi:linkRole"] == "RAW"
        ]
        self.assertEqual(len(raw_media), 1)

    def test_upload_updates_existing_material_appends_media(self):
        self.client.force_authenticate(user=self.user)
        # First upload creates the material.
        self.client.post(
            self.URL,
            {"file": self._pdf("a.pdf"), "material_type": "court_order"},
            format="multipart",
        )
        # Second upload updates it (no material_type needed) and appends.
        resp = self.client.post(
            self.URL, {"file": self._pdf("b.pdf"), "role": "ALTERNATE"}, format="multipart"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        media = resp.data["associatedMedia"]
        self.assertEqual(len(media), 2)
        self.assertEqual(media[1]["jawafdehi:linkRole"], "ALTERNATE")

    def test_upload_new_material_requires_material_type(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(self.URL, {"file": self._pdf()}, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("material_type", resp.data["detail"])
        self.assertFalse(Material.objects.filter(pk=self.IRI).exists())

    def test_raw_upload_enqueues_convert_by_default(self):
        # A convertible RAW upload triggers server-side OCR unless suppressed.
        from unittest.mock import patch

        self.client.force_authenticate(user=self.user)
        with patch("materials.conversion.enqueue_material_convert") as enq:
            resp = self.client.post(
                self.URL,
                {"file": self._pdf(), "material_type": "court_order"},
                format="multipart",
            )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        enq.assert_called_once_with(self.IRI)

    def test_skip_convert_suppresses_reocr(self):
        # A client with its own authoritative text passes skip_convert=1 so the
        # RAW upload does NOT enqueue material_convert (which would overwrite text).
        from unittest.mock import patch

        self.client.force_authenticate(user=self.user)
        with patch("materials.conversion.enqueue_material_convert") as enq:
            resp = self.client.post(
                self.URL,
                {"file": self._pdf(), "material_type": "court_order", "skip_convert": "1"},
                format="multipart",
            )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        enq.assert_not_called()

    def test_upload_missing_file_is_400(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            self.URL, {"material_type": "court_order"}, format="multipart"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("file", resp.data["detail"])

    def test_upload_invalid_role_is_400(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            self.URL,
            {"file": self._pdf(), "material_type": "court_order", "role": "BOGUS"},
            format="multipart",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_upload_oversize_is_413(self):
        # Guard against an unbounded stream to storage. DRF reconstructs the file
        # server-side (a client-set .size doesn't survive), so shrink the limit to
        # make a small file trip it — the code path (uploaded.size > limit) is the
        # same one a real 100 MB body would hit.
        from unittest.mock import patch

        self.client.force_authenticate(user=self.user)
        with patch("materials.views._MAX_UPLOAD_BYTES", 4):
            resp = self.client.post(
                self.URL,
                {"file": self._pdf("huge.pdf"), "material_type": "court_order"},
                format="multipart",
            )
        self.assertEqual(
            resp.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, resp.data
        )
        self.assertFalse(Material.objects.filter(pk=self.IRI).exists())

    def test_upload_unauth_is_401(self):
        resp = self.client.post(
            self.URL,
            {"file": self._pdf(), "material_type": "court_order"},
            format="multipart",
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_upload_authed_without_role_is_403(self):
        self.client.force_authenticate(user=self.norole)
        resp = self.client.post(
            self.URL,
            {"file": self._pdf(), "material_type": "court_order"},
            format="multipart",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_get_still_public_after_upload(self):
        self.client.force_authenticate(user=self.user)
        self.client.post(
            self.URL,
            {"file": self._pdf(), "material_type": "court_order"},
            format="multipart",
        )
        self.client.force_authenticate(user=None)
        resp = self.client.get("/api/materials/nkp/2080-order-1")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["@id"], self.IRI)
