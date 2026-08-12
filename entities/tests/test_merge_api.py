"""POST /api/entities/merge — the contract in the approved API spec."""

from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from cases.models import Case, CaseEntityRelationship, CaseState, CaseType, RelationshipType
from entities.models import StoredEntity
from entities.services.publication import PublicationService
from entities.write_validation import normalize_authoring_payload

User = get_user_model()

MERGE_URL = "/api/entities/merge"
JHAPA = "https://jawafdehi.org/entity/location/district/jhapa-np0104"
LOOSE = "https://jawafdehi.org/entity/location/jhapa"
KASKI = "https://jawafdehi.org/entity/location/district/kaski-np0439"


def _seed(prefix, slug, atype, **props):
    payload = {"prefix": prefix, "slug": slug, "type": atype,
               "name": {"en": "Jhapa", "ne": "झापा"}, **props}
    return PublicationService().create_entity(
        doc=normalize_authoring_payload(payload), author_id="oidc:seed",
        change_description="seed",
    )


class EntityMergeApiTests(APITestCase):
    databases = "__all__"

    @classmethod
    def setUpTestData(cls):
        group, _ = Group.objects.get_or_create(name="Caseworker")
        cls.caseworker = User.objects.create(username="oidc-sub-caseworker")
        cls.caseworker.groups.add(group)
        cls.norole = User.objects.create(username="oidc-sub-norole")

    def setUp(self):
        _seed("location/district", "jhapa-np0104",
              ["AdministrativeArea", "jawafdehi:District"],
              identifier=[{"@type": "PropertyValue", "propertyID": "ocha-pcode",
                           "value": "NP0104"}])
        _seed("location", "jhapa", "Place", description={"ne": "झापा जिल्ला"})
        self.client.force_authenticate(user=self.caseworker)

    def _post(self, **body):
        body.setdefault("survivor", JHAPA)
        body.setdefault("duplicates", [LOOSE])
        return self.client.post(MERGE_URL, body, format="json")

    def _case_with_bind(self, slug, nes_id, rel=RelationshipType.LOCATION):
        case = Case.objects.create(
            title="Jhapa land revenue case", slug=slug, case_type=CaseType.CORRUPTION,
            state=CaseState.DRAFT, short_description="t", description="t",
        )
        CaseEntityRelationship.objects.create(case=case, nes_id=nes_id, relationship_type=rel)
        return case

    # 1 — happy path
    def test_happy_path(self):
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["status"], "complete")
        dup = StoredEntity.objects.get(pk=LOOSE)
        self.assertTrue(dup.is_deleted)
        self.assertEqual(dup.merged_into, JHAPA)
        self.assertEqual(resp.data["survivor"]["description"], {"ne": "झापा जिल्ला"})

    # 2 — relationship transfer, and nothing left pointing at the retired entity
    def test_every_bind_moves_and_nothing_still_points_at_the_retired_entity(self):
        for n in range(3):
            self._case_with_bind(f"jhapa-case-{n}", LOOSE)
        resp = self._post()
        self.assertEqual(resp.data["references"]["case_entity_binds"]["repointed"], 3)
        self.assertEqual(CaseEntityRelationship.objects.filter(nes_id=JHAPA).count(), 3)
        self.assertFalse(CaseEntityRelationship.objects.filter(nes_id=LOOSE).exists())

    # 3 — both sides carry links, union with no duplication
    def test_union_with_no_duplication(self):
        case = self._case_with_bind("jhapa-both", JHAPA)
        CaseEntityRelationship.objects.create(
            case=case, nes_id=LOOSE, relationship_type=RelationshipType.LOCATION
        )
        resp = self._post()
        self.assertEqual(resp.data["references"]["case_entity_binds"]["deduplicated"], 1)
        self.assertEqual(CaseEntityRelationship.objects.filter(case=case).count(), 1)

    # 4 — multiple duplicates in one call
    def test_multiple_duplicates(self):
        _seed("location", "jhapa-alt", "Place")
        resp = self._post(duplicates=[LOOSE, "https://jawafdehi.org/entity/location/jhapa-alt"])
        self.assertEqual(resp.data["status"], "complete")
        self.assertEqual(len(resp.data["retired"]), 2)
        self.assertEqual(StoredEntity.objects.filter(merged_into=JHAPA).count(), 2)

    # 5 — no relationships at all
    def test_merge_with_no_references(self):
        resp = self._post()
        self.assertEqual(resp.data["total_references"], 0)
        self.assertEqual(resp.data["status"], "complete")

    # 6 — idempotency
    def test_rerun_is_a_safe_noop(self):
        self._post()
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["status"], "already_merged")
        self.assertEqual(resp.data["total_references"], 0)

    # 7 — wrong type
    def test_cross_family_merge_is_rejected(self):
        _seed("person", "ram-bahadur", "Person")
        resp = self._post(duplicates=["https://jawafdehi.org/entity/person/ram-bahadur"])
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(resp.data["error"]["code"], "TYPE_MISMATCH")

    def test_place_and_administrative_area_are_accepted(self):
        # The real production pair must NOT be rejected by the type check.
        self.assertEqual(self._post().status_code, status.HTTP_200_OK)

    # 8 — self merge
    def test_self_merge_is_rejected(self):
        resp = self._post(duplicates=[JHAPA])
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(resp.data["error"]["code"], "SELF_MERGE")

    # 9 — already retired
    def test_already_retired_duplicate_is_graceful(self):
        self._post()
        _seed("location/district", "kaski-np0439", "AdministrativeArea")
        resp = self._post(survivor=KASKI, duplicates=[LOOSE])
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(resp.data["error"]["code"], "DUPLICATE_ALREADY_MERGED")

    def test_merging_into_a_retired_survivor_is_rejected(self):
        self._post()
        _seed("location", "jhapa-older", "Place")
        resp = self._post(survivor=LOOSE,
                          duplicates=["https://jawafdehi.org/entity/location/jhapa-older"])
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(resp.data["error"]["code"], "SURVIVOR_RETIRED")

    # request validation
    def test_dry_run_writes_nothing(self):
        resp = self._post(dry_run=True)
        self.assertEqual(resp.data["status"], "planned")
        self.assertIsNone(resp.data["merge_id"])
        self.assertFalse(StoredEntity.objects.get(pk=LOOSE).is_deleted)

    def test_bare_prefix_slug_reference_is_rejected(self):
        resp = self._post(duplicates=["location/jhapa"])
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["error"]["code"], "INVALID_ENTITY_ID")

    def test_empty_duplicates_is_rejected(self):
        resp = self._post(duplicates=[])
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["error"]["code"], "INVALID_REQUEST")

    def test_over_twenty_five_duplicates_is_rejected(self):
        resp = self._post(duplicates=[f"{LOOSE}-{n}" for n in range(26)])
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["error"]["code"], "INVALID_REQUEST")

    def test_unknown_entity_is_a_404(self):
        resp = self._post(duplicates=["https://jawafdehi.org/entity/location/nowhere"])
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(resp.data["error"]["code"], "NOT_FOUND")

    # permissions
    def test_unauthenticated_is_401(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self._post().status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_without_the_caseworker_role_is_403(self):
        self.client.force_authenticate(user=self.norole)
        self.assertEqual(self._post().status_code, status.HTTP_403_FORBIDDEN)

    def test_a_tombstone_and_its_survivor_are_returned_once(self):
        self._post()
        resp = self.client.get(
            f"/api/entities?ids={quote(LOOSE, safe='')},{quote(JHAPA, safe='')}"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["total"], 1)
        self.assertEqual(len(resp.data["entities"]), 1)

    def test_the_merge_route_accepts_a_trailing_slash(self):
        resp = self.client.post(
            MERGE_URL + "/", {"survivor": JHAPA, "duplicates": [LOOSE]}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["status"], "complete")

    def test_a_merge_that_fails_partway_is_reported_as_partial(self):
        from entities.services.merge import service as svc

        original = svc.references.repoint_court_rows

        def _boom(*args, **kwargs):
            raise RuntimeError("ngm unreachable")

        svc.references.repoint_court_rows = _boom
        try:
            resp = self._post()
        finally:
            svc.references.repoint_court_rows = original
        self.assertEqual(resp.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(resp.data["status"], "partial")
        self.assertEqual(resp.data["error"]["code"], "MERGE_INCOMPLETE")
        self.assertTrue(resp.data["error"]["merge_id"])
