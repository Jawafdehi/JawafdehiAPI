"""Field-level write on the materials PATCH endpoint (RFC-6902 JSON Patch).

The endpoint previously accepted ONE thing — ``{"visibility_policy": ...}`` — so
the only way to change a key inside a material's stored JSON-LD was ``PUT``,
which replaces ``data`` wholesale and forces every client into a GET→merge→PUT
round-trip with no way to close the lost-update window. These tests pin the
field-level patch surface that replaces that: the same shape the NES entity write
plane already uses (``entities.write_validation`` / ``entities.views.patch``),
with the case endpoint's opt-in ``If-Match`` optimistic concurrency.

The existing visibility-policy contract must survive untouched — the moderation
UI is a live caller — so both body shapes are exercised side by side.
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from materials.models import Material, Policy, Visibility

User = get_user_model()

AG_IRI = "https://jawafdehi.org/material/ag/105334"


def _caseworker(username="cw-fieldpatch"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
    return u


def _client(username="cw-fieldpatch"):
    c = APIClient()
    c.force_authenticate(_caseworker(username))
    return c


def _store_ag(ident="105334", **extra):
    """An AG indictment material shaped like the ones already in the lake — the
    real cohort this endpoint exists to repair (no ``jawafdehi:caseNumber``)."""
    iri = f"https://jawafdehi.org/material/ag/{ident}"
    data = {
        "@id": iri,
        "@type": "DigitalDocument",
        "additionalType": "jawafdehi:ChargeSheet",
        "name": {"ne": "नेपाल सरकार विरुद्ध राम बहादुर थापा"},
        "jawafdehi:sourceType": "AG_ABHIYOG_PATRA",
        "publisher": {
            "@type": "GovernmentOrganization",
            "name": {"ne": "विशेष सरकारी वकील कार्यालय"},
        },
        **extra,
    }
    mat = Material(
        iri=iri,
        material_type="charge_sheet",
        source="ag",
        ident=ident,
        data=data,
        visibility=Visibility.LISTED,
    )
    mat.save()
    return mat


def _add_case_no(value="082-CR-0100"):
    return [{"op": "add", "path": "/jawafdehi:caseNumber", "value": value}]


@pytest.mark.django_db
class TestFieldPatch:
    def test_patch_ops_sets_a_field_in_the_stored_doc(self):
        mat = _store_ag()
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": _add_case_no()},
            format="json",
        )
        assert resp.status_code == 200
        mat.refresh_from_db()
        assert mat.data["jawafdehi:caseNumber"] == "082-CR-0100"

    def test_patch_ops_leaves_every_other_key_untouched(self):
        # The whole point of a field-level write: a PUT would have replaced the
        # document, so an incomplete client body silently drops publisher/name.
        mat = _store_ag(ident="105335")
        before = dict(mat.data)
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": _add_case_no()},
            format="json",
        )
        assert resp.status_code == 200
        mat.refresh_from_db()
        assert mat.data["name"] == before["name"]
        assert mat.data["publisher"] == before["publisher"]
        assert mat.data["jawafdehi:sourceType"] == before["jawafdehi:sourceType"]

    def test_replace_overwrites_an_existing_value(self):
        mat = _store_ag(ident="105336", **{"jawafdehi:caseNumber": "081-CR-0001"})
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {
                "patch_ops": [
                    {
                        "op": "replace",
                        "path": "/jawafdehi:caseNumber",
                        "value": "082-CR-0100",
                    }
                ]
            },
            format="json",
        )
        assert resp.status_code == 200
        mat.refresh_from_db()
        assert mat.data["jawafdehi:caseNumber"] == "082-CR-0100"

    def test_response_returns_the_patched_document(self):
        mat = _store_ag(ident="105337")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": _add_case_no()},
            format="json",
        )
        assert resp.data["jawafdehi:caseNumber"] == "082-CR-0100"

    def test_detail_route_accepts_patch_ops_too(self):
        # Both routes funnel through one handler; the composite source/ident form
        # is the one a backfill script addresses (it holds the portal record id).
        mat = _store_ag(ident="105338")
        resp = _client().patch(
            "/api/materials/ag/105338",
            {"patch_ops": _add_case_no()},
            format="json",
        )
        assert resp.status_code == 200
        mat.refresh_from_db()
        assert mat.data["jawafdehi:caseNumber"] == "082-CR-0100"

    def test_bare_array_body_is_accepted(self):
        # The cases endpoint takes a bare RFC-6902 array; accept that spelling so
        # a client written against either convention interoperates.
        mat = _store_ag(ident="105339")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}", _add_case_no(), format="json"
        )
        assert resp.status_code == 200
        mat.refresh_from_db()
        assert mat.data["jawafdehi:caseNumber"] == "082-CR-0100"

    def test_remove_deletes_a_key(self):
        mat = _store_ag(ident="105340", **{"jawafdehi:caseNumber": "081-CR-0001"})
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": [{"op": "remove", "path": "/jawafdehi:caseNumber"}]},
            format="json",
        )
        assert resp.status_code == 200
        mat.refresh_from_db()
        assert "jawafdehi:caseNumber" not in mat.data


@pytest.mark.django_db
class TestVisibilityPolicyContractUnchanged:
    """The pre-existing body shape is a live caller (the moderation UI)."""

    def test_policy_only_body_still_works(self):
        mat = _store_ag(ident="205001")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"visibility_policy": "PRIVATE"},
            format="json",
        )
        assert resp.status_code == 200
        mat.refresh_from_db()
        assert mat.visibility_policy == Policy.PRIVATE

    def test_invalid_policy_is_still_400(self):
        mat = _store_ag(ident="205002")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"visibility_policy": "SORTA_PUBLIC"},
            format="json",
        )
        assert resp.status_code == 400

    def test_policy_and_patch_ops_apply_together(self):
        mat = _store_ag(ident="205003")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": _add_case_no(), "visibility_policy": "PRIVATE"},
            format="json",
        )
        assert resp.status_code == 200
        mat.refresh_from_db()
        assert mat.data["jawafdehi:caseNumber"] == "082-CR-0100"
        assert mat.visibility_policy == Policy.PRIVATE

    def test_empty_body_is_still_400(self):
        # Neither a policy nor patch ops: a no-op write must stay an explicit
        # error, never a silent 200 that a caller mistakes for success.
        mat = _store_ag(ident="205004")
        resp = _client().patch(f"/api/materials/?iri={mat.iri}", {}, format="json")
        assert resp.status_code == 400


@pytest.mark.django_db
class TestBlockedPaths:
    """Identity + server-owned keys are not patchable."""

    @pytest.mark.parametrize(
        "path",
        [
            "/@id",  # identity: the IRI is the join key other services store
            "/@context",
            "/@type",  # promoted material_type column would drift from the doc
            "/additionalType",
            "/visibility_policy",  # envelope control field, never doc content
            "/jawafdehi:visibility",  # server-derived read annotation
            "/jawafdehi:visibilityPolicy",
        ],
    )
    def test_blocked_path_is_422(self, path):
        # Ident derived from the parameter itself — hash() is salted per
        # interpreter (PYTHONHASHSEED), so a hashed ident changes every run
        # and can collide between parametrizations.
        mat = _store_ag(ident="blocked-" + re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-"))
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": [{"op": "replace", "path": path, "value": "x"}]},
            format="json",
        )
        assert resp.status_code == 422

    def test_blocked_path_leaves_the_document_unchanged(self):
        mat = _store_ag(ident="305999")
        before = dict(mat.data)
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {
                "patch_ops": [
                    {"op": "add", "path": "/jawafdehi:caseNumber", "value": "082-CR-1"},
                    {"op": "replace", "path": "/@id", "value": "https://x/material/a/b"},
                ]
            },
            format="json",
        )
        # The whole patch is rejected up front — an allowed op sharing the
        # request with a blocked one must not land on its own (a partially
        # applied patch is the failure mode RFC-6902 atomicity exists to prevent).
        assert resp.status_code == 422
        mat.refresh_from_db()
        assert mat.data == before

    def test_move_out_of_a_blocked_path_is_422(self):
        # `move`/`copy` read through `from` — guarding only `path` would let a
        # caller relocate the @id and mutate identity by the back door.
        mat = _store_ag(ident="305001")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": [{"op": "move", "from": "/@id", "path": "/identifier"}]},
            format="json",
        )
        assert resp.status_code == 422


@pytest.mark.django_db
class TestMalformedPatches:
    def test_unknown_op_is_422(self):
        mat = _store_ag(ident="405001")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": [{"op": "obliterate", "path": "/name", "value": "x"}]},
            format="json",
        )
        assert resp.status_code == 422

    def test_op_without_a_pointer_is_422(self):
        mat = _store_ag(ident="405002")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": [{"op": "add", "value": "x"}]},
            format="json",
        )
        assert resp.status_code == 422

    def test_empty_patch_ops_list_is_422(self):
        mat = _store_ag(ident="405003")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}", {"patch_ops": []}, format="json"
        )
        assert resp.status_code == 422

    def test_pointer_into_a_missing_parent_is_400(self):
        # A well-formed op that cannot be applied is the patch library's verdict,
        # not a schema violation → 400, matching the entity write plane.
        mat = _store_ag(ident="405004")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": [{"op": "replace", "path": "/nope/deeper", "value": "x"}]},
            format="json",
        )
        assert resp.status_code == 400

    def test_patch_that_invalidates_the_document_is_422(self):
        # `name` is required by validate_material_jsonld; a patch may not leave
        # the stored doc in a state the write plane would have rejected.
        mat = _store_ag(ident="405005")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": [{"op": "remove", "path": "/name"}]},
            format="json",
        )
        assert resp.status_code == 422
        mat.refresh_from_db()
        assert mat.data["name"]


@pytest.mark.django_db
class TestFieldPatchAuth:
    def test_anon_is_401(self):
        mat = _store_ag(ident="505001")
        resp = APIClient().patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": _add_case_no()},
            format="json",
        )
        assert resp.status_code == 401

    def test_roleless_authed_is_403(self):
        mat = _store_ag(ident="505002")
        client = APIClient()
        client.force_authenticate(User.objects.create_user("nobody-fp", password="x"))
        resp = client.patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": _add_case_no()},
            format="json",
        )
        assert resp.status_code == 403

    def test_missing_material_is_404(self):
        resp = _client().patch(
            "/api/materials/?iri=https://jawafdehi.org/material/ag/nosuchrecord",
            {"patch_ops": _add_case_no()},
            format="json",
        )
        assert resp.status_code == 404


@pytest.mark.django_db
class TestOptimisticConcurrency:
    """Opt-in ``If-Match``, mirroring the case PATCH endpoint.

    Without it a backfill has to do a client-side read-modify-write and can
    silently clobber a concurrent caseworker edit. With it, a stale writer is
    told to reload instead.
    """

    def test_get_emits_an_etag(self):
        mat = _store_ag(ident="605001")
        resp = _client().get(f"/api/materials/?iri={mat.iri}")
        assert resp.status_code == 200
        assert resp["ETag"]

    def test_patch_response_carries_the_new_etag(self):
        mat = _store_ag(ident="605002")
        before = _client().get(f"/api/materials/?iri={mat.iri}")["ETag"]
        resp = _client("cw-fp2").patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": _add_case_no()},
            format="json",
        )
        assert resp.status_code == 200
        assert resp["ETag"] and resp["ETag"] != before

    def test_matching_if_match_is_applied(self):
        mat = _store_ag(ident="605003")
        client = _client()
        token = client.get(f"/api/materials/?iri={mat.iri}")["ETag"]
        resp = client.patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": _add_case_no()},
            format="json",
            HTTP_IF_MATCH=token,
        )
        assert resp.status_code == 200
        mat.refresh_from_db()
        assert mat.data["jawafdehi:caseNumber"] == "082-CR-0100"

    def test_stale_if_match_is_412_and_does_not_write(self):
        mat = _store_ag(ident="605004")
        client = _client()
        stale = client.get(f"/api/materials/?iri={mat.iri}")["ETag"]
        # Someone else edits in between.
        client.patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": _add_case_no("081-FT-0009")},
            format="json",
        )
        resp = client.patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": _add_case_no("082-CR-9999")},
            format="json",
            HTTP_IF_MATCH=stale,
        )
        assert resp.status_code == 412
        assert resp["ETag"]
        mat.refresh_from_db()
        assert mat.data["jawafdehi:caseNumber"] == "081-FT-0009"

    def test_star_if_match_matches_any_live_material(self):
        mat = _store_ag(ident="605005")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": _add_case_no()},
            format="json",
            HTTP_IF_MATCH="*",
        )
        assert resp.status_code == 200

    def test_no_if_match_header_still_writes(self):
        # Backward compatible: the precondition is opt-in, not required.
        mat = _store_ag(ident="605006")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": _add_case_no()},
            format="json",
        )
        assert resp.status_code == 200
