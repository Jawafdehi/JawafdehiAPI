"""Adversarial: the material field-level PATCH as an attack surface.

``PATCH /api/materials/...`` writes INTO a stored JSON-LD document, which makes
it a narrower verb than ``PUT`` but not a safer one. Four things it must never
become:

  * a way for a read-capable-but-not-write-capable principal to write;
  * a way to repoint or alias a material's identity (the IRI is the join key
    ``CaseMaterialReference`` stores — repointing it hijacks inbound references);
  * a way to lift a material's visibility gate by writing the server-owned
    annotations into the document body;
  * a way to CREATE or REVIVE a document (that is ``PUT``'s job, and revival of a
    soft-deleted material is exactly what an operator used ``DELETE`` to prevent).

Plus the usual malformed-input surface: no adversarial body may reach a 500.

Run with: ``uv run pytest -m security``.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from materials.models import Material, Policy, Visibility
from tests.conftest import create_user_with_role

pytestmark = [pytest.mark.security, pytest.mark.django_db]


def _store(ident, *, visibility=Visibility.LISTED, policy=Policy.PUBLIC, deleted=False):
    iri = f"https://jawafdehi.org/material/ag/{ident}"
    mat = Material(
        iri=iri,
        material_type="charge_sheet",
        source="ag",
        ident=ident,
        data={
            "@id": iri,
            "@type": "DigitalDocument",
            "name": {"ne": "अभियोगपत्र"},
            "publisher": {"@type": "GovernmentOrganization", "name": {"ne": "कार्यालय"}},
        },
        visibility=visibility,
        visibility_policy=policy,
        is_deleted=deleted,
    )
    mat.save()
    return mat


def _client(role, username):
    c = APIClient()
    c.force_authenticate(create_user_with_role(username, f"{username}@x.test", role))
    return c


def _ops(path="/jawafdehi:caseNumber", value="082-CR-0100", op="add"):
    return {"patch_ops": [{"op": op, "path": path, "value": value}]}


class TestRoleEscalation:
    """ReadOnly may READ non-public materials — it must not thereby WRITE them."""

    @pytest.mark.parametrize("role", ["Public", "ReadOnly"])
    def test_low_privilege_patch_is_403(self, role):
        mat = _store(f"sec-role-{role.lower()}")
        resp = _client(role, f"u-{role.lower()}-mp").patch(
            f"/api/materials/?iri={mat.iri}", _ops(), format="json"
        )
        assert resp.status_code == 403
        mat.refresh_from_db()
        assert "jawafdehi:caseNumber" not in mat.data

    def test_anonymous_patch_is_401(self):
        mat = _store("sec-anon")
        resp = APIClient().patch(
            f"/api/materials/?iri={mat.iri}", _ops(), format="json"
        )
        assert resp.status_code == 401
        mat.refresh_from_db()
        assert "jawafdehi:caseNumber" not in mat.data

    def test_jobpoller_cannot_patch(self):
        # JobPoller is a machine principal for the job queue; it is authenticated
        # and grouped, but it is not an NGM content role.
        mat = _store("sec-jobpoller")
        resp = _client("JobPoller", "u-poller-mp").patch(
            f"/api/materials/?iri={mat.iri}", _ops(), format="json"
        )
        assert resp.status_code == 403
        mat.refresh_from_db()
        assert "jawafdehi:caseNumber" not in mat.data

    def test_superuser_may_patch(self):
        # The platform-wide contract (HasNgmRole): a superuser bypasses the group
        # check. Pinned here so the set of principals that can write to the lake
        # is asserted, not assumed.
        mat = _store("sec-superuser")
        resp = _client("Admin", "u-admin-mp").patch(
            f"/api/materials/?iri={mat.iri}", _ops(), format="json"
        )
        assert resp.status_code == 200
        mat.refresh_from_db()
        assert mat.data["jawafdehi:caseNumber"] == "082-CR-0100"

    def test_anonymous_patch_of_a_malformed_iri_is_401_not_400(self):
        # The role gate must fire BEFORE input validation, so an unauthenticated
        # prober cannot use the difference between 400 and 404 to map the IRI
        # grammar or probe which materials exist.
        resp = APIClient().patch(
            "/api/materials/?iri=not-an-iri", _ops(), format="json"
        )
        assert resp.status_code == 401


class TestIdentityTampering:
    def test_repointing_id_at_another_material_is_rejected(self):
        victim = _store("sec-victim")
        attacker = _store("sec-attacker")
        victim_before, attacker_before = dict(victim.data), dict(attacker.data)

        resp = _client("Caseworker", "cw-idor-mp").patch(
            f"/api/materials/?iri={attacker.iri}",
            _ops(path="/@id", value=victim.iri, op="replace"),
            format="json",
        )
        assert resp.status_code == 422
        victim.refresh_from_db()
        attacker.refresh_from_db()
        assert victim.data == victim_before
        assert attacker.data == attacker_before

    def test_moving_the_id_away_is_rejected(self):
        # `move` REMOVES from its source — guarding only `path` and not `from`
        # would let identity be mutated by relocation.
        mat = _store("sec-move-id")
        before = dict(mat.data)
        resp = _client("Caseworker", "cw-move-mp").patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": [{"op": "move", "from": "/@id", "path": "/identifier"}]},
            format="json",
        )
        assert resp.status_code == 422
        mat.refresh_from_db()
        assert mat.data == before

    def test_retyping_the_document_is_rejected(self):
        # material_type is a promoted column the PATCH path does NOT re-derive;
        # letting @type drift from it would desynchronise the row from its doc.
        mat = _store("sec-retype")
        resp = _client("Caseworker", "cw-retype-mp").patch(
            f"/api/materials/?iri={mat.iri}",
            _ops(path="/@type", value="Person", op="replace"),
            format="json",
        )
        assert resp.status_code == 422
        mat.refresh_from_db()
        assert mat.material_type == "charge_sheet"
        assert mat.data["@type"] == "DigitalDocument"


class TestVisibilityGate:
    def test_writing_the_visibility_annotation_into_the_doc_is_rejected(self):
        # A PRIVATE material 404s for anon. Smuggling `jawafdehi:visibility` into
        # the stored document must not lift that gate — the cached column is the
        # only thing the read plane consults, but a document that CLAIMS to be
        # listed is a lie the admin UI would render.
        mat = _store("sec-vis", visibility=Visibility.PRIVATE, policy=Policy.PRIVATE)
        resp = _client("Caseworker", "cw-vis-mp").patch(
            f"/api/materials/?iri={mat.iri}",
            _ops(path="/jawafdehi:visibility", value="LISTED"),
            format="json",
        )
        assert resp.status_code == 422
        mat.refresh_from_db()
        assert mat.visibility == Visibility.PRIVATE
        assert "jawafdehi:visibility" not in mat.data
        assert APIClient().get(f"/api/materials/?iri={mat.iri}").status_code == 404

    def test_writing_the_policy_into_the_doc_is_rejected(self):
        mat = _store("sec-pol", visibility=Visibility.PRIVATE, policy=Policy.PRIVATE)
        resp = _client("Caseworker", "cw-pol-mp").patch(
            f"/api/materials/?iri={mat.iri}",
            _ops(path="/visibility_policy", value="PUBLIC"),
            format="json",
        )
        assert resp.status_code == 422
        mat.refresh_from_db()
        assert mat.visibility_policy == Policy.PRIVATE


class TestPatchIsNeverACreateOrRevive:
    def test_patching_a_soft_deleted_material_is_404_and_does_not_revive_it(self):
        # `upsert_single_source_material` deliberately REVIVES a soft-deleted @id
        # on PUT (a re-source republishes). PATCH must not inherit that: an
        # operator who DELETEd a document did so to take it down.
        mat = _store("sec-deleted", deleted=True)
        resp = _client("Caseworker", "cw-del-mp").patch(
            f"/api/materials/?iri={mat.iri}", _ops(), format="json"
        )
        assert resp.status_code == 404
        mat.refresh_from_db()
        assert mat.is_deleted is True
        assert "jawafdehi:caseNumber" not in mat.data

    def test_patching_an_unknown_iri_creates_nothing(self):
        iri = "https://jawafdehi.org/material/ag/sec-does-not-exist"
        resp = _client("Caseworker", "cw-create-mp").patch(
            f"/api/materials/?iri={iri}", _ops(), format="json"
        )
        assert resp.status_code == 404
        assert not Material.objects.filter(pk=iri).exists()

    def test_patching_a_derived_court_case_material_creates_nothing(self):
        # A court-case IRI RESOLVES on GET (materialized on the fly) but has no
        # stored row. PATCH must 404 rather than silently materialize one.
        iri = "https://jawafdehi.org/material/court/special.080-cr-0100"
        resp = _client("Caseworker", "cw-derived-mp").patch(
            f"/api/materials/?iri={iri}", _ops(), format="json"
        )
        assert resp.status_code == 404
        assert not Material.objects.filter(pk=iri).exists()


class TestMalformedInputNeverFivehundreds:
    """Every adversarial body is a 4xx. A 500 is an availability bug and leaks
    a traceback through the error reporter."""

    @pytest.mark.parametrize(
        "body",
        [
            {"patch_ops": "not-a-list"},
            {"patch_ops": {"op": "add"}},
            {"patch_ops": 42},
            {"patch_ops": [None]},
            {"patch_ops": [{"op": "add", "path": 5, "value": 1}]},
            {"patch_ops": [{"op": "add", "path": "no-leading-slash", "value": 1}]},
            {"patch_ops": [{"op": "move", "path": "/x"}]},  # missing `from`
            {"patch_ops": [{"op": "copy", "from": 3, "path": "/x"}]},
            {"patch_ops": [{"op": "remove"}]},
            {"patch_ops": [{"op": "replace", "path": "/name"}]},  # missing value
            "a bare string",
            42,
        ],
    )
    def test_adversarial_body_is_4xx(self, body):
        mat = _store("sec-malformed")
        resp = _client("Caseworker", "cw-mal-mp").patch(
            f"/api/materials/?iri={mat.iri}", body, format="json"
        )
        assert 400 <= resp.status_code < 500, f"{body!r} → {resp.status_code}"
        mat.refresh_from_db()
        assert "jawafdehi:caseNumber" not in mat.data

    def test_a_failing_test_op_is_400_not_500(self):
        # RFC-6902 `test` is a client-side assertion; when it fails the patch
        # library raises, and that must surface as a 400 rather than escaping as
        # an unhandled exception.
        mat = _store("sec-testop")
        resp = _client("Caseworker", "cw-testop-mp").patch(
            f"/api/materials/?iri={mat.iri}",
            {
                "patch_ops": [
                    {"op": "test", "path": "/name", "value": {"ne": "wrong"}},
                    {"op": "add", "path": "/jawafdehi:caseNumber", "value": "082-CR-1"},
                ]
            },
            format="json",
        )
        assert resp.status_code == 400
        mat.refresh_from_db()
        assert "jawafdehi:caseNumber" not in mat.data

    def test_a_deeply_nested_pointer_does_not_crash(self):
        mat = _store("sec-deep")
        resp = _client("Caseworker", "cw-deep-mp").patch(
            f"/api/materials/?iri={mat.iri}",
            _ops(path="/a" * 200 + "/x"),
            format="json",
        )
        assert 400 <= resp.status_code < 500

    def test_a_rejected_patch_leaves_nested_content_untouched(self):
        # apply_patch runs with in_place=False; if it aliased the stored dict, a
        # patch rejected AFTER application (document validation) would still have
        # mutated nested objects in memory — and the next save would persist them.
        mat = _store("sec-nested")
        before = dict(mat.data)
        resp = _client("Caseworker", "cw-nested-mp").patch(
            f"/api/materials/?iri={mat.iri}",
            {
                "patch_ops": [
                    {"op": "replace", "path": "/publisher/name", "value": {"ne": "X"}},
                    {"op": "remove", "path": "/name"},  # → document invalid → 422
                ]
            },
            format="json",
        )
        assert resp.status_code == 422
        mat.refresh_from_db()
        assert mat.data == before
