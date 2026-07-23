"""Regressions found reviewing the field-level PATCH.

Three of these are consequences of one change: wrapping the material write in
``transaction.atomic(using="ngm")``. That block is what makes the read-modify-write
safe, but it also means anything that assumed the save ran in autocommit — most
importantly the search-index ``on_commit`` hook — now has to name the same alias
or it fires at the wrong time. The rest are read-plane details the ETag introduced.
"""

from __future__ import annotations

from unittest.mock import patch as mock_patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connections, transaction
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from materials.models import Material, Visibility

User = get_user_model()
pytestmark = pytest.mark.django_db(databases=["default", "ngm"], transaction=True)


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


def _client(username="cw-review"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
    c = APIClient()
    c.force_authenticate(u)
    return c


class TestIndexingIsDeferredUntilTheMaterialIsDurable:
    def test_index_write_waits_for_the_ngm_commit(self):
        # `Material` lives on `ngm`. If the on_commit hook registers against the
        # DEFAULT alias it resolves against a connection that is in autocommit
        # here, so Django runs it IMMEDIATELY — publishing a document to search
        # that the ngm transaction has not committed yet (and might roll back),
        # while the row lock is still held across an OpenSearch round-trip.
        mat = _store("rev-001")
        with mock_patch("materials.search_index.index") as indexer:
            with transaction.atomic(using="ngm"):
                mat.data = {**mat.data, "jawafdehi:caseNumber": "082-CR-0100"}
                mat.save(update_fields=["data", "updated_at"])
                assert not indexer.called, (
                    "search index was written before the ngm transaction committed"
                )
            assert indexer.called, "search index was never written after commit"

    def test_a_rolled_back_write_is_never_indexed(self):
        mat = _store("rev-002")
        with mock_patch("materials.search_index.index") as indexer:
            with pytest.raises(RuntimeError):
                with transaction.atomic(using="ngm"):
                    mat.data = {**mat.data, "jawafdehi:caseNumber": "082-CR-0100"}
                    mat.save(update_fields=["data", "updated_at"])
                    raise RuntimeError("boom")
            assert not indexer.called


class TestEtagComesFromTheAuthority:
    def test_the_etag_is_read_from_the_primary_not_the_router(self):
        # The token is a write PRECONDITION, so it must be minted from the same
        # database `_patch_material` validates it against. `/api/materials/` is
        # NOT in config.middleware._PRIMARY_ONLY_PREFIXES, so an unpinned read is
        # replica-eligible; a lagging replica would hand out a stale token and
        # produce a 412 loop on an edit nobody else touched.
        #
        # Simulated by pointing the router somewhere the row does not exist: an
        # unpinned _stored_etag follows the router and finds nothing (None); a
        # pinned one still reads ngm and returns the real token.
        from config.db_router import ServiceDatabaseRouter
        from materials.views import _stored_etag

        mat = _store("rev-010")
        with mock_patch.object(
            ServiceDatabaseRouter, "db_for_read", return_value="default"
        ):
            token = _stored_etag(mat.iri)
        assert token, "_stored_etag followed the router instead of the primary"


class TestReadPlaneEfficiency:
    def test_a_get_reads_the_material_row_once(self):
        mat = _store("rev-020")
        client = APIClient()
        with CaptureQueriesContext(connections["ngm"]) as ctx:
            resp = client.get(f"/api/materials/?iri={mat.iri}")
        assert resp.status_code == 200
        selects = [
            q for q in ctx.captured_queries
            if "materials" in q["sql"].lower() and q["sql"].lower().startswith("select")
        ]
        assert len(selects) == 1, [q["sql"] for q in selects]

    def test_a_derived_court_material_does_not_query_for_an_etag(self):
        # _resolve_material already proved there is no stored row by taking the
        # DoesNotExist branch; re-asking can never return anything.
        iri = "https://jawafdehi.org/material/court/special.080-cr-9999"
        client = APIClient()
        with CaptureQueriesContext(connections["ngm"]) as ctx:
            client.get(f"/api/materials/?iri={iri}")
        selects = [
            q for q in ctx.captured_queries
            if "materials" in q["sql"].lower() and q["sql"].lower().startswith("select")
        ]
        assert len(selects) == 1, [q["sql"] for q in selects]


class TestEtagDoesNotLeakTheAuthedRepresentation:
    def test_the_response_varies_on_authorization(self):
        # One URL serves two different bodies — anon gets `row.data`, a caseworker
        # gets it plus jawafdehi:visibility[Policy] — under ONE entity tag. Without
        # Vary, a shared cache keyed on URL+ETag may serve the annotated body to an
        # anonymous caller, disclosing the material's visibility policy.
        mat = _store("rev-030")
        anon = APIClient().get(f"/api/materials/?iri={mat.iri}")
        authed = _client().get(f"/api/materials/?iri={mat.iri}")
        assert anon.status_code == authed.status_code == 200
        assert "jawafdehi:visibilityPolicy" in authed.data
        assert "jawafdehi:visibilityPolicy" not in anon.data
        for resp in (anon, authed):
            assert "authorization" in resp.get("Vary", "").lower(), resp.get("Vary")


class TestPatchStillGoesThroughModelValidation:
    def test_a_patch_runs_the_model_layer_clean(self):
        # The PATCH path writes `data` directly rather than through
        # upsert_single_source_material, so Material.clean() — which re-checks the
        # promoted source/ident columns against @id — must be invoked explicitly
        # or the model-layer invariant silently stops applying on this verb.
        mat = _store("rev-040")
        with mock_patch.object(
            Material, "full_clean", autospec=True
        ) as full_clean:
            resp = _client("cw-clean").patch(
                f"/api/materials/?iri={mat.iri}",
                {"patch_ops": [{"op": "add", "path": "/x", "value": 1}]},
                format="json",
            )
        assert resp.status_code == 200
        assert full_clean.called


class TestWriteSizeIsBounded:
    """A patch may not grow a row without limit.

    `DATA_UPLOAD_MAX_MEMORY_SIZE` does NOT cover this path: DRF's JSONParser
    reads the WSGI stream directly instead of going through `HttpRequest.body`,
    where Django's check lives. Measured: a 3.6 MB body against a 2.5 MB limit
    returned 200 and persisted. So the bound has to be explicit — this is the
    one Material write path that had no ceiling, while the upload endpoint next
    door is capped at `_MAX_UPLOAD_BYTES`.
    """

    def test_too_many_ops_is_413(self):
        from materials.patch_validation import MAX_PATCH_OPS

        mat = _store("rev-050")
        ops = [
            {"op": "add", "path": f"/k{i}", "value": i}
            for i in range(MAX_PATCH_OPS + 1)
        ]
        resp = _client("cw-ops").patch(
            f"/api/materials/?iri={mat.iri}", {"patch_ops": ops}, format="json"
        )
        assert resp.status_code == 413
        mat.refresh_from_db()
        assert "k0" not in mat.data

    def test_a_patch_that_would_oversize_the_document_is_413(self):
        from materials.patch_validation import MAX_MATERIAL_DOC_BYTES

        mat = _store("rev-051")
        resp = _client("cw-big").patch(
            f"/api/materials/?iri={mat.iri}",
            {
                "patch_ops": [
                    {"op": "add", "path": "/text",
                     "value": "k" * (MAX_MATERIAL_DOC_BYTES + 1024)}
                ]
            },
            format="json",
        )
        assert resp.status_code == 413
        mat.refresh_from_db()
        assert "text" not in mat.data

    def test_a_realistic_full_text_document_still_fits(self):
        # An AG indictment embeds its likhit-converted full text; the cap must be
        # well clear of a genuinely large one, not merely of the tiny fixtures.
        mat = _store("rev-052")
        resp = _client("cw-real").patch(
            f"/api/materials/?iri={mat.iri}",
            {"patch_ops": [{"op": "add", "path": "/text",
                            "value": {"ne": "क" * 400_000}}]},
            format="json",
        )
        assert resp.status_code == 200
