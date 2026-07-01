"""DRF API tests for the NGM WRITE plane (court resources + materials).

Adds CREATE + UPDATE coverage to the existing read-plane tests. Auth mirrors
``test_api.py``: force-authenticate a user with the synced Django Group
"Caseworker" to pass ``HasNgmRole``; unauthenticated/garbage-credential paths
assert the 401/403 contract. ("ngm.query" scope is query-only, NOT a write grant.)

Run under the platform settings (DB-less: sqlite fallback, managed court tables)
from the repo root::

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest courts/tests/test_write_api.py
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from courts.models import BlacklistedFirm, Court, CourtCase
from materials.jsonld import MATERIAL_CONTEXT, court_case_material_iri
from materials.models import Material

User = get_user_model()

IRI = "https://jawafdehi.org/entity/person/ram-bahadur"
BAD_NES_ID = "entity:person/ram-bahadur"  # legacy opaque form, not an IRI


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


# ── Court write ──────────────────────────────────────────────────────────────


class CourtWriteTests(_DbAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = cls._make_caseworker()

    def test_create_court_authed(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            "/api/ngm/courts/",
            {
                "identifier": "patandc",
                "court_type": "district",
                "full_name_nepali": "जिल्ला अदालत पाटन",
                "full_name_english": "District Court Patan",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Court.objects.filter(identifier="patandc").exists())

    def test_update_court_patch(self):
        Court.objects.create(
            identifier="patandc", court_type="district", full_name_nepali="x"
        )
        self.client.force_authenticate(user=self.user)
        resp = self.client.patch(
            "/api/ngm/courts/patandc/",
            {"full_name_english": "District Court Patan"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            Court.objects.get(pk="patandc").full_name_english, "District Court Patan"
        )

    def test_unauth_create_is_401(self):
        resp = self.client.post(
            "/api/ngm/courts/",
            {"identifier": "x", "court_type": "d", "full_name_nepali": "x"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_read_courts_still_public(self):
        Court.objects.create(identifier="sc", court_type="supreme", full_name_nepali="स")
        resp = self.client.get("/api/ngm/courts/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


# ── CourtCase write (composite PK) ─────────────────────────────────────────────


class CourtCaseWriteTests(_DbAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = cls._make_caseworker()
        cls.norole = cls._make_norole()
        cls.court = Court.objects.create(
            identifier="kathmandudc", court_type="district", full_name_nepali="ज"
        )

    def _payload(self, **overrides):
        body = {
            "case_number": "082-OA-0503",
            "court_identifier": "kathmandudc",
            "case_type": "भ्रष्टाचार",
            "case_status": "चालु",
            "plaintiff": "राम",
            "defendant": "श्याम",
            "nes_id": IRI,
            "document_sources": [
                {"document_id": "d1", "url": [{"link": "https://r2/raw.pdf", "role": "RAW"}]}
            ],
        }
        body.update(overrides)
        return body

    def test_create_case_then_read_roundtrip(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post("/api/ngm/cases/", self._payload(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["case_number"], "082-OA-0503")
        self.assertEqual(resp.data["court_identifier"], "kathmandudc")
        # Read serializer fields present on the create response.
        self.assertIn("material_id", resp.data)
        self.assertIn("courtcase_iri", resp.data)

        # Round-trips via the composite read endpoint.
        read = self.client.get("/api/ngm/cases/kathmandudc/082-OA-0503")
        self.assertEqual(read.status_code, status.HTTP_200_OK)
        self.assertEqual(read.data["nes_id"], IRI)
        self.assertEqual(read.data["plaintiff"], "राम")

    def test_update_case_patch_composite(self):
        CourtCase.objects.create(
            case_number="082-OA-0503", court=self.court, case_status="चालु"
        )
        self.client.force_authenticate(user=self.user)
        resp = self.client.patch(
            "/api/ngm/cases/kathmandudc/082-OA-0503",
            {"case_status": "फैसला", "defendant": "हरि"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        case = CourtCase.objects.get(court_id="kathmandudc", case_number="082-OA-0503")
        self.assertEqual(case.case_status, "फैसला")
        self.assertEqual(case.defendant, "हरि")

    def test_update_case_put_composite(self):
        CourtCase.objects.create(
            case_number="082-OA-0503", court=self.court, case_status="चालु"
        )
        self.client.force_authenticate(user=self.user)
        resp = self.client.put(
            "/api/ngm/cases/kathmandudc/082-OA-0503", self._payload(), format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        case = CourtCase.objects.get(court_id="kathmandudc", case_number="082-OA-0503")
        self.assertEqual(case.nes_id, IRI)

    def test_create_unauth_is_401(self):
        resp = self.client.post("/api/ngm/cases/", self._payload(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_create_authed_without_role_is_403(self):
        self.client.force_authenticate(user=self.norole)
        resp = self.client.post("/api/ngm/cases/", self._payload(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_update_unauth_is_401(self):
        CourtCase.objects.create(
            case_number="082-OA-0503", court=self.court, case_status="चालु"
        )
        resp = self.client.patch(
            "/api/ngm/cases/kathmandudc/082-OA-0503",
            {"case_status": "फैसला"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_create_invalid_nes_id_is_400(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            "/api/ngm/cases/", self._payload(nes_id=BAD_NES_ID), format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("nes_id", resp.data)

    def test_create_blank_nes_id_allowed(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            "/api/ngm/cases/", self._payload(nes_id=""), format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)


# ── BlacklistedFirm write ──────────────────────────────────────────────────────


class BlacklistedFirmWriteTests(_DbAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = cls._make_caseworker()

    def test_create_firm_authed(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            "/api/ngm/firms/",
            {"firm_name": "Acme Builders", "blacklist_date_bs": "2080-01-01"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertTrue(BlacklistedFirm.objects.filter(firm_name="Acme Builders").exists())

    def test_update_firm_patch(self):
        firm = BlacklistedFirm.objects.create(firm_name="Acme Builders")
        self.client.force_authenticate(user=self.user)
        resp = self.client.patch(
            f"/api/ngm/firms/{firm.id}/", {"reason": "fraud"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(BlacklistedFirm.objects.get(pk=firm.id).reason, "fraud")

    def test_create_unauth_is_401(self):
        resp = self.client.post(
            "/api/ngm/firms/", {"firm_name": "X"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


# ── Material write ─────────────────────────────────────────────────────────────


class MaterialWriteTests(_DbAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = cls._make_caseworker()
        cls.norole = cls._make_norole()

    def _doc(self, iri="https://jawafdehi.org/material/nkp/2080-act-1"):
        return {
            "@context": MATERIAL_CONTEXT,
            "@type": "Legislation",
            "@id": iri,
            "name": {"ne": "ऐन"},
        }

    def test_create_material_then_get_by_iri(self):
        self.client.force_authenticate(user=self.user)
        iri = "https://jawafdehi.org/material/nkp/2080-act-1"
        resp = self.client.post("/api/ngm/materials/", self._doc(iri), format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["@id"], iri)
        self.assertTrue(Material.objects.filter(pk=iri).exists())
        self.assertEqual(Material.objects.get(pk=iri).material_type, "legal_corpus")

        # GET-resolvable by IRI (query form) AND by path component.
        self.client.force_authenticate(user=None)
        by_iri = self.client.get("/api/ngm/materials/", {"iri": iri})
        self.assertEqual(by_iri.status_code, status.HTTP_200_OK)
        self.assertEqual(by_iri.data["@type"], "Legislation")
        by_path = self.client.get("/api/ngm/materials/nkp/2080-act-1")
        self.assertEqual(by_path.status_code, status.HTTP_200_OK)

    def test_create_material_envelope_with_explicit_type(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            "/api/ngm/materials/",
            {"material": self._doc(), "material_type": "official_report"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(
            Material.objects.get(pk=self._doc()["@id"]).material_type, "official_report"
        )

    def test_put_material_replaces(self):
        iri = "https://jawafdehi.org/material/nkp/2080-act-1"
        Material.objects.create(
            iri=iri, material_type="legal_corpus", source="nkp", ident="2080-act-1",
            data=self._doc(iri),
        )
        self.client.force_authenticate(user=self.user)
        new_doc = self._doc(iri)
        new_doc["name"] = {"ne": "संशोधित ऐन"}
        resp = self.client.put("/api/ngm/materials/nkp/2080-act-1", new_doc, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(Material.objects.get(pk=iri).data["name"], {"ne": "संशोधित ऐन"})

    def test_put_iri_mismatch_is_400(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.put(
            "/api/ngm/materials/nkp/2080-act-1",
            self._doc("https://jawafdehi.org/material/nkp/other"),
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_invalid_jsonld_is_400(self):
        self.client.force_authenticate(user=self.user)
        # Unknown @type -> validator rejects.
        resp = self.client.post(
            "/api/ngm/materials/",
            {"@type": "Banana", "@id": "https://jawafdehi.org/material/x/y", "name": "n"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_unauth_is_401(self):
        resp = self.client.post("/api/ngm/materials/", self._doc(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_create_authed_without_role_is_403(self):
        self.client.force_authenticate(user=self.norole)
        resp = self.client.post("/api/ngm/materials/", self._doc(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_get_material_remains_public_no_regression(self):
        iri = "https://jawafdehi.org/material/nkp/2080-act-1"
        Material.objects.create(
            iri=iri, material_type="legal_corpus", source="nkp", ident="2080-act-1",
            data=self._doc(iri),
        )
        # No auth.
        resp = self.client.get("/api/ngm/materials/nkp/2080-act-1")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["@id"], iri)

    def test_get_derived_court_case_material_still_works(self):
        # The on-the-fly court-case fallback must keep working after adding POST.
        court = Court.objects.create(
            identifier="kathmandudc", court_type="district", full_name_nepali="ज"
        )
        CourtCase.objects.create(
            case_number="082-OA-0503", court=court, case_status="चालु",
            document_sources=[
                {"document_id": "d1", "url": [{"link": "https://r2/raw.pdf", "role": "RAW"}]}
            ],
        )
        iri = court_case_material_iri("kathmandudc", "082-OA-0503")
        resp = self.client.get("/api/ngm/materials/", {"iri": iri})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["additionalType"], "jawafdehi:CourtCase")
