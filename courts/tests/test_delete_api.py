"""DRF API tests for the CourtCase SOFT-DELETE plane (composite key).

``DELETE /api/courtcases/{court}/{case_number}`` flips ``is_deleted=True``
(never hard-delete — accountability/audit platform): the case vanishes from the
list + composite detail but the row survives. Writes are NGM-role gated (the
synced ``Caseworker`` Django Group passes ``HasNgmRole``); unauth /
authenticated-without-role assert the 401 / 403 contract.

Run under the platform settings (DB-less: sqlite fallback) from the repo root::

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest courts/tests/test_delete_api.py
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from courts.models import Court, CourtCase

User = get_user_model()


class CourtCaseDeleteTests(APITestCase):
    databases = "__all__"

    @classmethod
    def setUpTestData(cls):
        g, _ = Group.objects.get_or_create(name="Caseworker")
        cls.user = User.objects.create(username="oidc-writer")
        cls.user.groups.add(g)
        cls.norole = User.objects.create(username="oidc-norole")
        cls.court = Court.objects.create(
            identifier="kathmandudc", court_type="district", full_name_nepali="ज"
        )

    def setUp(self):
        CourtCase.objects.create(
            case_number="082-OA-0503", court=self.court, case_status="चालु"
        )

    def _get(self):
        return CourtCase.objects.get(
            court_id="kathmandudc", case_number="082-OA-0503"
        )

    def test_delete_soft_deletes_and_returns_204(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.delete("/api/courtcases/kathmandudc/082-OA-0503")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        # Row still exists (never hard-deleted); flagged soft-deleted.
        self.assertTrue(self._get().is_deleted)

    def test_deleted_hidden_from_detail(self):
        self.client.force_authenticate(user=self.user)
        self.client.delete("/api/courtcases/kathmandudc/082-OA-0503")
        self.client.force_authenticate(user=None)
        resp = self.client.get("/api/courtcases/kathmandudc/082-OA-0503")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_deleted_hidden_from_list(self):
        self.client.force_authenticate(user=self.user)
        self.client.delete("/api/courtcases/kathmandudc/082-OA-0503")
        self.client.force_authenticate(user=None)
        resp = self.client.get("/api/courtcases/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["results"], [])

    def test_delete_unknown_is_404(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.delete("/api/courtcases/kathmandudc/no-such-case")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_unauth_is_401(self):
        resp = self.client.delete("/api/courtcases/kathmandudc/082-OA-0503")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(self._get().is_deleted)

    def test_delete_authed_without_role_is_403(self):
        self.client.force_authenticate(user=self.norole)
        resp = self.client.delete("/api/courtcases/kathmandudc/082-OA-0503")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(self._get().is_deleted)
