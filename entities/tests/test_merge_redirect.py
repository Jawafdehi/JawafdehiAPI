"""A retired entity's URL 301-redirects to its survivor."""

from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from entities.models import StoredEntity
from entities.services.publication import PublicationService
from entities.write_validation import normalize_authoring_payload

User = get_user_model()

JHAPA = "https://jawafdehi.org/entity/location/district/jhapa-np0104"
LOOSE = "https://jawafdehi.org/entity/location/jhapa"


def _seed(prefix, slug, atype):
    return PublicationService().create_entity(
        doc=normalize_authoring_payload(
            {"prefix": prefix, "slug": slug, "type": atype, "name": {"en": "Jhapa"}}
        ),
        author_id="oidc:seed", change_description="seed",
    )


class RetiredEntityRedirectTests(APITestCase):
    databases = "__all__"

    @classmethod
    def setUpTestData(cls):
        group, _ = Group.objects.get_or_create(name="Caseworker")
        cls.caseworker = User.objects.create(username="oidc-sub-caseworker")
        cls.caseworker.groups.add(group)

    def setUp(self):
        _seed("location/district", "jhapa-np0104", "AdministrativeArea")
        _seed("location", "jhapa", "Place")
        self.client.force_authenticate(user=self.caseworker)
        resp = self.client.post(
            "/api/entities/merge",
            {"survivor": JHAPA, "duplicates": [LOOSE]}, format="json",
        )
        assert resp.status_code == status.HTTP_200_OK, resp.data
        self.client.force_authenticate(user=None)

    def test_detail_redirects_to_the_survivor(self):
        resp = self.client.get(f"/api/entities/{quote(LOOSE, safe='')}")
        self.assertEqual(resp.status_code, status.HTTP_301_MOVED_PERMANENTLY)
        self.assertIn(quote(JHAPA, safe=""), resp["Location"])

    def test_prefix_slug_form_also_redirects(self):
        resp = self.client.get("/api/entities/location/jhapa")
        self.assertEqual(resp.status_code, status.HTTP_301_MOVED_PERMANENTLY)

    def test_versions_route_redirects(self):
        resp = self.client.get(f"/api/entities/{quote(LOOSE, safe='')}/versions")
        self.assertEqual(resp.status_code, status.HTTP_301_MOVED_PERMANENTLY)

    def test_the_survivor_still_serves_normally(self):
        resp = self.client.get(f"/api/entities/{quote(JHAPA, safe='')}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["@id"], JHAPA)

    def test_a_genuinely_unknown_entity_is_still_a_404(self):
        resp = self.client.get("/api/entities/location/nowhere")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_batch_lookup_reports_the_redirect_and_returns_the_survivor(self):
        resp = self.client.get(f"/api/entities?ids={quote(LOOSE, safe='')}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["total"], 1)
        self.assertEqual(resp.data["entities"][0]["@id"], JHAPA)
        self.assertEqual(resp.data["redirected"][LOOSE], JHAPA)
        self.assertNotIn("not_found", resp.data)

    def test_a_plainly_soft_deleted_entity_404s_instead_of_redirecting(self):
        # DELETE writes no pointer, so its row is not a tombstone.
        _seed("location", "jhapa-deleted", "Place")
        deleted = "https://jawafdehi.org/entity/location/jhapa-deleted"
        row = StoredEntity.objects.get(pk=deleted)
        row.is_deleted = True
        row.save(update_fields=["is_deleted"])
        resp = self.client.get(f"/api/entities/{quote(deleted, safe='')}")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_live_entity_carrying_the_pointer_does_not_redirect(self):
        # The pointer alone is not a tombstone: the row must be soft-deleted too, so a
        # pointer left on a live row cannot hijack its URL.
        _seed("location", "jhapa-live", "Place")
        live = "https://jawafdehi.org/entity/location/jhapa-live"
        row = StoredEntity.objects.get(pk=live)
        row.merged_into = JHAPA
        row.save(update_fields=["merged_into"])
        resp = self.client.get(f"/api/entities/{quote(live, safe='')}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["@id"], live)

    def test_a_survivor_that_was_later_deleted_does_not_redirect(self):
        survivor_row = StoredEntity.objects.get(pk=JHAPA)
        survivor_row.is_deleted = True
        survivor_row.save(update_fields=["is_deleted"])
        resp = self.client.get(f"/api/entities/{quote(LOOSE, safe='')}")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
