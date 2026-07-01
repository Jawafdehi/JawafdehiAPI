"""DRF API tests for the NGM read plane + gated query + search stub.

Run under the platform settings (DB-less: sqlite fallback, managed court
tables) from the repo root:

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest courts/tests

Auth: the gated planes use the shared OIDCAuthentication (Zitadel JWT -> Django
Groups). Rather than mint real JWTs, the authorized-path tests force-authenticate
a user with the synced Groups; the unauthorized-path tests send no/garbage
credentials and assert the 401/403 contract.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from courts.models import (
    BlacklistedFirm,
    CaseEntity,
    Court,
    CourtCase,
    CourtCaseHearing,
)

User = get_user_model()


class _DbAPITestCase(APITestCase):
    """APITestCase that may touch every database.

    Under the unified settings the DB router pins the NGM
    ``courts``/``materials`` models to the ``ngm`` alias while auth.User lives in
    ``default``; Django's test runner only sets up ``default`` unless a test
    declares the databases it uses. ``"__all__"`` enrolls every alias so the
    routed ``ngm`` queries are allowed in tests.
    """

    databases = "__all__"


class ReadPlaneTests(_DbAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.court = Court.objects.create(
            identifier="kathmandudc",
            court_type="district",
            full_name_nepali="जिल्ला अदालत काठमाडौं",
            full_name_english="District Court Kathmandu",
        )
        cls.case = CourtCase.objects.create(
            case_number="082-OA-0503",
            court=cls.court,
            case_type="भ्रष्टाचार",
            case_status="चालु",
            plaintiff="X",
            defendant="Y",
            document_sources=[{"document_id": "ngm:doc:1", "url": [{"link": "u", "role": "RAW"}]}],
        )
        CourtCaseHearing.objects.create(
            case_number="082-OA-0503",
            court=cls.court,
            hearing_date_bs="2082-09-28",
            hearing_date_ad=date(2026, 1, 11),
            scraped_at=datetime(2026, 1, 11, tzinfo=timezone.utc),
        )
        CaseEntity.objects.create(
            case_number="082-OA-0503",
            court=cls.court,
            side="plaintiff",
            name="Ram Bahadur",
            nes_id="https://jawafdehi.org/entity/person/ram-bahadur",
        )
        BlacklistedFirm.objects.create(
            firm_name="Acme Builders",
            blacklist_date_bs="2080-01-01",
            blacklist_date_ad=date(2023, 4, 14),
        )

    def test_health(self):
        # NGM's per-plane health was dropped in the unified-surface cutover; the
        # single canonical /api/health (entities.urls) serves the whole platform.
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_list_courts_public(self):
        resp = self.client.get("/api/courts/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["identifier"], "kathmandudc")

    def test_list_cases_paginated_shape(self):
        resp = self.client.get("/api/courtcases/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Platform cursor pagination -> {results, next}
        self.assertIn("results", resp.data)
        self.assertIn("next", resp.data)
        self.assertEqual(len(resp.data["results"]), 1)

    def test_case_detail_composite_key(self):
        resp = self.client.get("/api/courtcases/kathmandudc/082-OA-0503")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["case_number"], "082-OA-0503")
        self.assertEqual(resp.data["court_identifier"], "kathmandudc")
        # The synthesized court-case @id IRI (derived from the composite key,
        # court+case_number lowercased), distinct from the material IRI.
        self.assertEqual(
            resp.data["courtcase_iri"],
            "https://jawafdehi.org/courtcase/kathmandudc/082-oa-0503",
        )

    def test_case_detail_normalizes_loose_case_number(self):
        # lowercase + Devanagari digits should normalize to the stored form.
        resp = self.client.get("/api/courtcases/kathmandudc/82-oa-503")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["case_number"], "082-OA-0503")

    def test_case_sub_resources(self):
        h = self.client.get("/api/courtcases/kathmandudc/082-OA-0503/hearings")
        self.assertEqual(h.status_code, status.HTTP_200_OK)
        self.assertEqual(len(h.data["results"]), 1)

        e = self.client.get("/api/courtcases/kathmandudc/082-OA-0503/entities")
        self.assertEqual(e.status_code, status.HTTP_200_OK)
        self.assertEqual(e.data["results"][0]["nes_id"], "https://jawafdehi.org/entity/person/ram-bahadur")

        d = self.client.get("/api/courtcases/kathmandudc/082-OA-0503/documents")
        self.assertEqual(d.status_code, status.HTTP_200_OK)
        self.assertEqual(d.data["results"][0]["document_id"], "ngm:doc:1")

    def test_firms_public(self):
        resp = self.client.get("/api/firms/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["results"][0]["firm_name"], "Acme Builders")

    def test_entities_search(self):
        resp = self.client.get("/api/courtcase-entities/", {"nes_id": "https://jawafdehi.org/entity/person/ram-bahadur"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["results"]), 1)


class SearchRetiredTests(_DbAPITestCase):
    """The NGM 501 search stub was retired in the unified-search cutover.

    NGM has no search route of its own; platform search lives at the ``search``
    app (``GET /api/search/``). The old NGM-scoped ``courtcases/search`` never
    existed post-cutover.
    """

    def test_ngm_has_no_own_search_route(self):
        resp = self.client.get("/api/courtcases/search/", {"q": "bribery"})
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class QueryGateTests(_DbAPITestCase):
    @classmethod
    def setUpTestData(cls):
        Court.objects.create(
            identifier="supreme", court_type="supreme", full_name_nepali="सर्वोच्च अदालत"
        )
        cls.contrib_group, _ = Group.objects.get_or_create(name="Caseworker")
        cls.user = User.objects.create(username="oidc-sub-123")
        cls.user.groups.add(cls.contrib_group)
        cls.nobody = User.objects.create(username="oidc-sub-norole")

    def test_unauth_is_401(self):
        resp = self.client.post("/api/query/", {"query": "SELECT 1"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_bogus_token_is_401(self):
        # A Bearer attempt with an unverifiable token -> authenticator 401.
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-jwt")
        resp = self.client.post("/api/query/", {"query": "SELECT 1"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authed_without_ngm_role_is_403(self):
        self.client.force_authenticate(user=self.nobody)
        resp = self.client.post("/api/query/", {"query": "SELECT 1"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_select_is_400(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            "/api/query/", {"query": "DELETE FROM court_cases"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_disallowed_table_is_400(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            "/api/query/", {"query": "SELECT * FROM scraped_dates"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_valid_select_runs(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            "/api/query/", {"query": "SELECT identifier FROM courts"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("rows", resp.data)
        self.assertEqual(resp.data["max_rows"], 500)

    def test_ngm_query_scope_grants_access_without_role(self):
        # A principal with no NGM role but bearing the ``ngm.query`` OAuth scope
        # (the FastAPI scope control) is accepted — scope OR role suffices.
        self.client.force_authenticate(
            user=self.nobody, token={"scope": "openid ngm.query"}
        )
        resp = self.client.post(
            "/api/query/", {"query": "SELECT identifier FROM courts"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_other_scope_without_role_is_403(self):
        # An authenticated principal with neither the scope nor an NGM role -> 403.
        self.client.force_authenticate(
            user=self.nobody, token={"scope": "openid profile"}
        )
        resp = self.client.post("/api/query/", {"query": "SELECT 1"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class IngestionGateTests(_DbAPITestCase):
    @classmethod
    def setUpTestData(cls):
        g, _ = Group.objects.get_or_create(name="Caseworker")
        cls.user = User.objects.create(username="ingest-user")
        cls.user.groups.add(g)

    def test_unauth_ingestion_is_401(self):
        resp = self.client.post("/api/ingestion/cases/", {"items": []}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authed_ingestion_requires_items_list(self):
        # The ingestion writers are now REAL: a body with no `items` list is a
        # 400 (not the former 501 stub). Full behavior is covered by
        # courts/tests/test_ingestion_api.py.
        self.client.force_authenticate(user=self.user)
        for path in (
            "/api/ingestion/cases/",
            "/api/ingestion/entities/resolve/",
            "/api/ingestion/documents/",
        ):
            resp = self.client.post(path, {}, format="json")
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, msg=path)

    def test_resolve_rejects_non_iri_nes_id(self):
        # Clean-slate: the resolve write plane IRI-validates before write-back.
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            "/api/ingestion/entities/resolve/",
            {"items": [{"nes_id": "entity:person/ram"}]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resolve_accepts_iri_nes_id_unmatched(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            "/api/ingestion/entities/resolve/",
            {
                "items": [
                    {
                        "court": "kathmandudc",
                        "case_number": "no-such-case",
                        "nes_id": "https://jawafdehi.org/entity/person/ram",
                    }
                ]
            },
            format="json",
        )
        # Valid IRI passes the gate; no matching party rows -> 200 unmatched.
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["unmatched"], 1)


class LakehouseImportTests(_DbAPITestCase):
    """The lakehouse layer must import cleanly with no live infra (stubs raise)."""

    def test_modules_import_and_schema_is_well_formed(self):
        from lakehouse import engine, medallion, schema

        self.assertIn("court_cases", schema.SILVER_TABLES)
        # Natural key declared for the court_cases silver projection.
        self.assertEqual(
            schema.get_table("court_cases").natural_key,
            ["court_identifier", "case_number"],
        )
        # The live boundaries are stubbed.
        with self.assertRaises(NotImplementedError):
            medallion.refresh_gold()
        # build_attach_sql is a pure renderer; engine.connect ATTACH is stubbed.
        self.assertTrue(hasattr(engine, "build_attach_sql"))
