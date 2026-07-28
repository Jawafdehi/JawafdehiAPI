"""API tests for the real ``/api/ingestion/*`` bulk writers.

Covers the ported ingestion plane: idempotent bulk case upsert, document-source
registration, and nes_id write-back onto case parties. All are NGM-role gated
(auth setup mirrors ``courts/tests/test_write_api.py``).

Run (DB-less: sqlite fallback, managed court tables) from the repo root::

    SECRET_KEY=dev ALLOWED_HOSTS='*' uv run pytest courts/tests/test_ingestion_api.py
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from courts.models import BlacklistedFirm, CaseEntity, Court, CourtCase

User = get_user_model()

IRI = "https://jawafdehi.org/entity/person/ram-bahadur"
BAD_NES_ID = "entity:person/ram-bahadur"


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


class IngestionCasesTests(_DbAPITestCase):
    URL = "/api/ingestion/cases/"

    @classmethod
    def setUpTestData(cls):
        cls.user = cls._make_caseworker()
        cls.norole = cls._make_norole()
        cls.court = Court.objects.create(
            identifier="kathmandudc", court_type="district", full_name_nepali="ज"
        )

    def _item(self, **overrides):
        item = {
            "court": "kathmandudc",
            "case_number": "082-OA-0503",
            "case_type": "भ्रष्टाचार",
            "case_status": "चालु",
        }
        item.update(overrides)
        return item

    def test_bulk_create_then_idempotent_rerun(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(self.URL, {"items": [self._item()]}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["created"], 1)
        self.assertEqual(resp.data["updated"], 0)
        self.assertTrue(
            CourtCase.objects.filter(court_id="kathmandudc", case_number="082-OA-0503").exists()
        )

        # Re-run the same batch -> updated, not created (idempotent).
        resp2 = self.client.post(
            self.URL, {"items": [self._item(case_status="फैसला")]}, format="json"
        )
        self.assertEqual(resp2.data["created"], 0)
        self.assertEqual(resp2.data["updated"], 1)
        case = CourtCase.objects.get(court_id="kathmandudc", case_number="082-OA-0503")
        self.assertEqual(case.case_status, "फैसला")
        # No duplicate row created.
        self.assertEqual(CourtCase.objects.filter(case_number="082-OA-0503").count(), 1)

    def test_bulk_mixed_and_bad_item(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            self.URL,
            {"items": [self._item(), {"case_number": "x"}]},  # 2nd missing court
            format="json",
        )
        self.assertEqual(resp.data["created"], 1)
        self.assertEqual(resp.data["failed"], 1)

    def test_bad_nes_id_is_item_failure(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            self.URL, {"items": [self._item(nes_id=BAD_NES_ID)]}, format="json"
        )
        self.assertEqual(resp.data["failed"], 1)
        self.assertEqual(resp.data["created"], 0)

    def test_missing_items_is_400(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(self.URL, {"foo": []}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauth_is_401(self):
        resp = self.client.post(self.URL, {"items": [self._item()]}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authed_without_role_is_403(self):
        self.client.force_authenticate(user=self.norole)
        resp = self.client.post(self.URL, {"items": [self._item()]}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class IngestionDocumentsTests(_DbAPITestCase):
    URL = "/api/ingestion/documents/"

    @classmethod
    def setUpTestData(cls):
        cls.user = cls._make_caseworker()
        cls.court = Court.objects.create(
            identifier="kathmandudc", court_type="district", full_name_nepali="ज"
        )
        cls.case = CourtCase.objects.create(
            case_number="082-OA-0503", court=cls.court, case_status="चालु"
        )

    def _item(self, document_id="d1", **overrides):
        item = {
            "court": "kathmandudc",
            "case_number": "082-OA-0503",
            "document_source": {
                "document_id": document_id,
                "url": [{"link": "https://r2/raw.pdf", "role": "RAW"}],
            },
        }
        item.update(overrides)
        return item

    def test_register_document_appends_and_is_idempotent(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(self.URL, {"items": [self._item()]}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["updated"], 1)
        case = CourtCase.objects.get(court_id="kathmandudc", case_number="082-OA-0503")
        self.assertEqual(len(case.document_sources), 1)

        # Re-register same document_id -> replaced in place, not duplicated.
        self.client.post(self.URL, {"items": [self._item()]}, format="json")
        case.refresh_from_db()
        self.assertEqual(len(case.document_sources), 1)

    def test_unmatched_case(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            self.URL, {"items": [self._item(case_number="NOPE")]}, format="json"
        )
        self.assertEqual(resp.data["unmatched"], 1)

    def test_unauth_is_401(self):
        resp = self.client.post(self.URL, {"items": [self._item()]}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class IngestionEntitiesResolveTests(_DbAPITestCase):
    URL = "/api/ingestion/entities/resolve/"

    @classmethod
    def setUpTestData(cls):
        cls.user = cls._make_caseworker()
        cls.court = Court.objects.create(
            identifier="kathmandudc", court_type="district", full_name_nepali="ज"
        )
        cls.entity = CaseEntity.objects.create(
            case_number="082-OA-0503", court=cls.court, side="plaintiff", name="राम"
        )

    def test_resolve_writes_back_nes_id(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            self.URL,
            {"items": [{"court": "kathmandudc", "case_number": "082-OA-0503", "nes_id": IRI}]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["resolved"], 1)
        self.entity.refresh_from_db()
        self.assertEqual(self.entity.nes_id, IRI)

    def test_resolve_reindexes_parent_courtcase(self):
        # The write-back is a bulk .update() (bypasses the CaseEntity post_save
        # reindex signal), so the parent CourtCase — whose search doc folds party
        # nes_id IRIs into ``identifiers`` — must be re-indexed explicitly on
        # commit. Assert search_index.index() is called with the parent case.
        from unittest.mock import patch

        CourtCase.objects.create(court=self.court, case_number="082-OA-0503")
        self.client.force_authenticate(user=self.user)
        with patch("courts.views.search_index.index") as mock_index:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(
                    self.URL,
                    {"items": [{"court": "kathmandudc", "case_number": "082-OA-0503", "nes_id": IRI}]},
                    format="json",
                )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(mock_index.called, "parent CourtCase was not re-indexed")
        reindexed = mock_index.call_args[0][0]
        self.assertEqual(reindexed.case_number, "082-OA-0503")

    def test_resolve_filters_by_side_and_name(self):
        CaseEntity.objects.create(
            case_number="082-OA-0503", court=self.court, side="defendant", name="श्याम"
        )
        self.client.force_authenticate(user=self.user)
        self.client.post(
            self.URL,
            {
                "items": [
                    {
                        "court": "kathmandudc",
                        "case_number": "082-OA-0503",
                        "side": "plaintiff",
                        "nes_id": IRI,
                    }
                ]
            },
            format="json",
        )
        self.entity.refresh_from_db()
        self.assertEqual(self.entity.nes_id, IRI)
        # The defendant row was NOT touched.
        self.assertIsNone(
            CaseEntity.objects.get(side="defendant", name="श्याम").nes_id
        )

    def test_bad_nes_id_rejects_batch_400(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            self.URL,
            {"items": [{"court": "kathmandudc", "case_number": "082-OA-0503", "nes_id": BAD_NES_ID}]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.entity.refresh_from_db()
        self.assertIsNone(self.entity.nes_id)

    def test_unmatched_when_no_party(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            self.URL,
            {"items": [{"court": "kathmandudc", "case_number": "NOPE", "nes_id": IRI}]},
            format="json",
        )
        self.assertEqual(resp.data["unmatched"], 1)

    def test_unauth_is_401(self):
        resp = self.client.post(
            self.URL,
            {"items": [{"court": "kathmandudc", "case_number": "082-OA-0503", "nes_id": IRI}]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class IngestionFirmsTests(_DbAPITestCase):
    URL = "/api/ingestion/firms/"

    @classmethod
    def setUpTestData(cls):
        cls.user = cls._make_caseworker()
        cls.norole = cls._make_norole()

    def _item(self, **overrides):
        item = {
            "firm_name": "एबीसी निर्माण सेवा",
            "blacklist_date_bs": "2078-05-08",
            "duration": "2078-05-08 to 2080-05-07",
        }
        item.update(overrides)
        return item

    def test_norole_is_403(self):
        self.client.force_authenticate(user=self.norole)
        resp = self.client.post(self.URL, {"items": [self._item()]}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_missing_natural_key_is_failed(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            self.URL, {"items": [{"firm_name": "No Date Co"}]}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data["failed"], 1)
        self.assertFalse(BlacklistedFirm.objects.filter(firm_name="No Date Co").exists())

    def test_create_then_backfill_then_no_overwrite(self):
        self.client.force_authenticate(user=self.user)

        # create (no address yet)
        resp = self.client.post(self.URL, {"items": [self._item()]}, format="json")
        self.assertEqual(resp.data["created"], 1, resp.data)
        firm = BlacklistedFirm.objects.get(firm_name="एबीसी निर्माण सेवा")
        self.assertEqual(firm.blacklist_date_bs, "2078-05-08")
        self.assertIsNone(firm.address)

        # re-run with address -> back-filled (updated)
        resp = self.client.post(
            self.URL, {"items": [self._item(address="काठमाडौं")]}, format="json"
        )
        self.assertEqual(resp.data["updated"], 1)
        firm.refresh_from_db()
        self.assertEqual(firm.address, "काठमाडौं")

        # re-run with a DIFFERENT address -> a present value is never overwritten
        resp = self.client.post(
            self.URL, {"items": [self._item(address="ललितपुर")]}, format="json"
        )
        self.assertEqual(resp.data["unchanged"], 1)
        firm.refresh_from_db()
        self.assertEqual(firm.address, "काठमाडौं")
        self.assertEqual(BlacklistedFirm.objects.filter(firm_name="एबीसी निर्माण सेवा").count(), 1)
