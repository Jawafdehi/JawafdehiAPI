"""Contract conformance for the material field-level PATCH.

Separate from the functional suite on purpose: these assert the endpoint's
*published* contract — the status codes, headers, media type and header-parsing
tolerances a client is entitled to rely on, plus the fact that the OpenAPI schema
actually advertises the operation. A functional test proves the feature works; a
contract test proves the thing we told clients is the thing we built.

The ``If-Match`` tolerances (weak validator, unquoted token, comma-separated
list) come from RFC 7232 §3.1 and are asserted explicitly because they are the
kind of detail that quietly regresses when a header parser is "simplified".
"""

from __future__ import annotations

import re

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from materials.models import Material, Visibility

User = get_user_model()

pytestmark = pytest.mark.django_db


def _client(username="cw-contract"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
    c = APIClient()
    c.force_authenticate(u)
    return c


def _store(ident):
    iri = f"https://jawafdehi.org/material/ag/{ident}"
    mat = Material(
        iri=iri,
        material_type="charge_sheet",
        source="ag",
        ident=ident,
        data={"@id": iri, "@type": "DigitalDocument", "name": {"ne": "अभियोगपत्र"}},
        visibility=Visibility.LISTED,
    )
    mat.save()
    return mat


def _ops(value="082-CR-0100"):
    return {"patch_ops": [{"op": "add", "path": "/jawafdehi:caseNumber", "value": value}]}


@pytest.fixture(scope="module")
def schema():
    """The generated OpenAPI document — built once, it is not cheap."""
    from drf_spectacular.generators import SchemaGenerator

    return SchemaGenerator().get_schema(request=None, public=True)


class TestResponseContract:
    def test_success_is_ld_json(self):
        mat = _store("c-001")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}", _ops(), format="json"
        )
        assert resp.status_code == 200
        assert resp["Content-Type"].startswith("application/ld+json")

    def test_body_is_the_stored_doc_plus_exactly_two_annotations(self):
        mat = _store("c-002")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}", _ops(), format="json"
        )
        mat.refresh_from_db()
        assert set(resp.data) - set(mat.data) == {
            "jawafdehi:visibility",
            "jawafdehi:visibilityPolicy",
        }
        assert {k: v for k, v in resp.data.items() if k in mat.data} == mat.data

    def test_the_annotations_are_never_persisted_into_the_document(self):
        # The response is annotated; the stored document must not be. A client
        # doing GET → edit → PATCH would otherwise round-trip them into `data`.
        mat = _store("c-003")
        _client().patch(f"/api/materials/?iri={mat.iri}", _ops(), format="json")
        mat.refresh_from_db()
        assert "jawafdehi:visibility" not in mat.data
        assert "jawafdehi:visibilityPolicy" not in mat.data


class TestIdempotencyContract:
    def test_the_same_add_applied_twice_converges(self):
        mat = _store("c-010")
        client = _client()
        first = client.patch(f"/api/materials/?iri={mat.iri}", _ops(), format="json")
        second = client.patch(f"/api/materials/?iri={mat.iri}", _ops(), format="json")
        assert first.status_code == second.status_code == 200
        assert first.data == second.data

    def test_a_no_op_patch_does_not_bump_the_version(self):
        # `updated_at` drives the ETag; bumping it on a write that changed
        # nothing would invalidate every other client's token for no reason.
        mat = _store("c-011")
        client = _client()
        client.patch(f"/api/materials/?iri={mat.iri}", _ops(), format="json")
        before = client.get(f"/api/materials/?iri={mat.iri}")["ETag"]
        again = client.patch(f"/api/materials/?iri={mat.iri}", _ops(), format="json")
        assert again["ETag"] == before


class TestIfMatchHeaderContract:
    """RFC 7232 §3.1 tolerances the endpoint advertises."""

    def _token(self, client, mat):
        return client.get(f"/api/materials/?iri={mat.iri}")["ETag"]

    def test_weak_validator_prefix_is_accepted(self):
        mat = _store("c-020")
        client = _client()
        token = self._token(client, mat)
        resp = client.patch(
            f"/api/materials/?iri={mat.iri}",
            _ops(),
            format="json",
            HTTP_IF_MATCH=f"W/{token}",
        )
        assert resp.status_code == 200

    def test_unquoted_token_is_accepted(self):
        mat = _store("c-021")
        client = _client()
        token = self._token(client, mat).strip('"')
        resp = client.patch(
            f"/api/materials/?iri={mat.iri}",
            _ops(),
            format="json",
            HTTP_IF_MATCH=token,
        )
        assert resp.status_code == 200

    def test_a_comma_separated_list_matches_on_any_member(self):
        mat = _store("c-022")
        client = _client()
        token = self._token(client, mat)
        resp = client.patch(
            f"/api/materials/?iri={mat.iri}",
            _ops(),
            format="json",
            HTTP_IF_MATCH=f'"deadbeefdeadbeef", {token}',
        )
        assert resp.status_code == 200

    def test_a_list_matching_nothing_is_412(self):
        mat = _store("c-023")
        resp = _client().patch(
            f"/api/materials/?iri={mat.iri}",
            _ops(),
            format="json",
            HTTP_IF_MATCH='"deadbeefdeadbeef", "cafebabecafebabe"',
        )
        assert resp.status_code == 412

    def test_the_412_response_carries_the_current_token(self):
        # Without it the client cannot reconcile without an extra round-trip.
        mat = _store("c-024")
        client = _client()
        resp = client.patch(
            f"/api/materials/?iri={mat.iri}",
            _ops(),
            format="json",
            HTTP_IF_MATCH='"0000000000000000"',
        )
        assert resp.status_code == 412
        assert resp["ETag"] == self._token(client, mat)

    def test_the_etag_from_a_patch_is_directly_reusable_as_if_match(self):
        # The round-trip a backfill actually performs: patch, keep the returned
        # token, patch again conditionally without re-reading.
        mat = _store("c-025")
        client = _client()
        first = client.patch(f"/api/materials/?iri={mat.iri}", _ops(), format="json")
        second = client.patch(
            f"/api/materials/?iri={mat.iri}",
            _ops("082-CR-0200"),
            format="json",
            HTTP_IF_MATCH=first["ETag"],
        )
        assert second.status_code == 200
        mat.refresh_from_db()
        assert mat.data["jawafdehi:caseNumber"] == "082-CR-0200"


class _Probe:
    """Stand-in material used only to ask a ``url_for`` lambda which route it
    builds, so the fixture ident can be made deterministic per (route, body)."""

    iri = "probe"
    source = "probe-src"
    ident = "probe-id"


class TestBothRoutesAgree:
    """Two URLs address one resource; their PATCH contract must be identical."""

    @pytest.mark.parametrize(
        "url_for",
        [
            lambda m: f"/api/materials/?iri={m.iri}",
            lambda m: f"/api/materials/{m.source}/{m.ident}",
        ],
    )
    @pytest.mark.parametrize(
        "body,expected",
        [
            ({"patch_ops": [{"op": "add", "path": "/x", "value": 1}]}, 200),
            ({"patch_ops": [{"op": "add", "path": "/@id", "value": "x"}]}, 422),
            ({"patch_ops": [{"op": "nope", "path": "/x", "value": 1}]}, 422),
            ({"patch_ops": [{"op": "replace", "path": "/a/b", "value": 1}]}, 400),
            ({}, 400),
            ({"visibility_policy": "NOPE"}, 400),
        ],
    )
    def test_status_codes_match_across_routes(self, url_for, body, expected):
        # Deterministic per (route, body): hash() is salted per interpreter, so
        # a hashed ident/username changes every run and two parametrizations can
        # collide onto one Material PK or username.
        key = re.sub(r"[^a-z0-9]+", "-", f"{url_for(_Probe())}-{body}-{expected}".lower()).strip("-")[:60]
        mat = _store(f"c-30-{key}")
        resp = _client(f"cw-{key}"[:150]).patch(url_for(mat), body, format="json")
        assert resp.status_code == expected


class TestOpenApiSchemaAdvertisesThePatch:
    """The swagger page is how operators discover this endpoint — if the schema
    stops describing it, the endpoint is effectively undocumented."""

    @pytest.mark.parametrize(
        "path",
        ["/api/materials/", "/api/materials/{source}/{ident}/"],
    )
    def test_patch_operation_is_documented(self, schema, path):
        assert path in schema["paths"], sorted(schema["paths"])[:40]
        assert "patch" in schema["paths"][path]

    @pytest.mark.parametrize(
        "path",
        ["/api/materials/", "/api/materials/{source}/{ident}/"],
    )
    def test_the_documented_description_mentions_the_field_level_write(
        self, schema, path
    ):
        description = schema["paths"][path]["patch"].get("description", "")
        assert "patch_ops" in description
        assert "visibility_policy" in description
