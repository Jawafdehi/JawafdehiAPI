"""End-to-end (local-DB smoke): the CIAA shaper's doc through the REAL material API.

Drives ``PUT /api/materials/ciaa_press_release/<id>`` then ``POST …/file`` exactly
as ``scrape_ciaa_press_releases`` does — through the actual URLconf + NGM-role-gated
views, against the real (sqlite, managed materials table) DB. Asserts the Material
row lands with the shaped @id / dates / text / SOURCE_PAGE+RAW media, the /file
upload preserves the PUT metadata, and a re-PUT is idempotent (one row).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from materials.models import Material
from materials.sourcing.ciaa.parse import ParsedPressRelease
from materials.sourcing.ciaa.shaper import press_release_to_jsonld

User = get_user_model()


class CiaaPressIngestE2ETests(APITestCase):
    databases = "__all__"
    IRI = "https://jawafdehi.org/material/ciaa_press_release/3540"
    DOC_URL = "/api/materials/ciaa_press_release/3540"
    FILE_URL = "/api/materials/ciaa_press_release/3540/file"

    @classmethod
    def setUpTestData(cls):
        g, _ = Group.objects.get_or_create(name="Caseworker")
        cls.user = User.objects.create(username="oidc-writer")
        cls.user.groups.add(g)

    @staticmethod
    def _record():
        return ParsedPressRelease(
            press_id=3540,
            title="भ्रष्टाचार मुद्दा दायर सम्बन्धी प्रेस विज्ञप्ति",
            full_text="मिति २०८१।०९।२८ गते आयोगले मुद्दा दायर गरेको ।",
            publication_date_bs="2081-09-28",
            file_urls=["https://ciaa.gov.np/uploads//pressRelease/abc.pdf"],
            source_url="https://ciaa.gov.np/pressrelease/3540",
        )

    def test_put_then_file_lands_row_and_is_idempotent(self):
        self.client.force_authenticate(user=self.user)
        record = self._record()
        doc, material_type = press_release_to_jsonld(record)

        # 1) PUT the doc (mirrors MaterialApiClient.put_document).
        resp = self.client.put(
            self.DOC_URL, {"material": doc, "material_type": material_type}, format="json"
        )
        self.assertIn(resp.status_code, (200, 201), getattr(resp, "data", resp))

        row = Material.objects.get(pk=self.IRI)
        self.assertEqual(row.material_type, "press_release")
        self.assertEqual(row.source, "ciaa_press_release")
        self.assertEqual(row.ident, "3540")
        self.assertEqual(row.data["datePublished"], "2025-01-12")
        self.assertTrue(row.data["text"]["ne"].startswith("मिति"))
        self.assertIn(
            "SOURCE_PAGE",
            [m["jawafdehi:linkRole"] for m in row.data.get("associatedMedia", [])],
        )

        # 2) Upload the PDF attachment (mirrors MaterialApiClient.upload_file).
        pdf = SimpleUploadedFile("abc.pdf", b"%PDF-1.4 ...", content_type="application/pdf")
        up = self.client.post(
            self.FILE_URL,
            {
                "file": pdf, "role": "RAW", "material_type": material_type,
                "source_url": record.source_url, "skip_convert": "true",
            },
            format="multipart",
        )
        self.assertIn(up.status_code, (200, 201), getattr(up, "data", up))

        row.refresh_from_db()
        roles = [m["jawafdehi:linkRole"] for m in row.data["associatedMedia"]]
        self.assertIn("RAW", roles)                       # attachment attached
        self.assertIn("SOURCE_PAGE", roles)               # PUT metadata preserved
        self.assertTrue(row.data["text"]["ne"].startswith("मिति"))  # skip_convert kept text

        # 3) Re-PUT is idempotent — still exactly one row at the @id.
        again = self.client.put(
            self.DOC_URL, {"material": doc, "material_type": material_type}, format="json"
        )
        self.assertIn(again.status_code, (200, 201))
        self.assertEqual(Material.objects.filter(pk=self.IRI).count(), 1)

    def test_file_upload_requires_ngm_role(self):
        # Reader without the Caseworker group is 401/403 on the write (gate holds).
        norole = User.objects.create(username="oidc-norole")
        self.client.force_authenticate(user=norole)
        doc, material_type = press_release_to_jsonld(self._record())
        resp = self.client.put(
            self.DOC_URL, {"material": doc, "material_type": material_type}, format="json"
        )
        self.assertIn(resp.status_code, (401, 403))
        self.assertFalse(Material.objects.filter(pk=self.IRI).exists())
