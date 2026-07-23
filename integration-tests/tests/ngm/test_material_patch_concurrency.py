"""Live-stack proof that the material PATCH row lock prevents lost updates.

This is the one property the unit gate CANNOT show. ``ci.yml`` runs the whole
suite on in-memory sqlite, and SQLite sets ``has_select_for_update = False``, so
Django silently drops the ``FOR UPDATE`` clause — there is no lock there to
observe, and concurrent writers fail with sqlite's file-level "database is
locked" instead of queueing.

The integration job (``.github/workflows/integration.yml``) brings up the real
compose stack — Postgres included — and runs THIS package against it, so the
lock is exercised here for real.

What it asserts: N clients concurrently PATCH the SAME material, each adding a
DIFFERENT key. Every key must be present at the end. Under the old
``GET → merge → PUT`` shape each writer would send a whole document built from
its own stale snapshot and the last write would win, silently discarding the
others — with every request returning 200. A missing key here is exactly that
lost update.

Requires an NGM-role bearer token; skips (rather than fails) without one, so the
suite still runs on a stack without Zitadel wired up.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import make_client, skip_if_throttled

pytestmark = [pytest.mark.live]

BASE_URL = os.getenv("PLATFORM_BASE_URL") or os.getenv(
    "NGM_API_BASE_URL", "http://localhost:48000"
)

WRITERS = 8


@pytest.fixture(scope="module")
def authed(oidc_token):
    if not oidc_token:
        pytest.skip("no OIDC token — material writes are NGM-role gated")
    with make_client(BASE_URL, headers={"Authorization": f"Bearer {oidc_token}"}) as c:
        yield c


@pytest.fixture
def material(authed):
    """Create a throwaway material, yield its IRI, soft-delete it afterwards."""
    ident = f"patch-race-{uuid.uuid4().hex[:12]}"
    iri = f"https://jawafdehi.org/material/ag/{ident}"
    doc = {
        "@id": iri,
        "@type": "DigitalDocument",
        "additionalType": "jawafdehi:ChargeSheet",
        "name": {"ne": "नेपाल सरकार विरुद्ध राम बहादुर थापा"},
        "jawafdehi:sourceType": "AG_ABHIYOG_PATRA",
    }
    r = authed.put(f"/api/materials/ag/{ident}", json=doc)
    skip_if_throttled(r)
    if r.status_code in (401, 403):
        pytest.skip(f"token lacks the NGM write role ({r.status_code})")
    assert r.status_code in (200, 201), r.text
    yield ident, iri
    authed.delete(f"/api/materials/?iri={iri}")


def test_concurrent_patches_do_not_lose_writes(authed, material):
    ident, iri = material

    def add_key(i: int):
        return authed.patch(
            f"/api/materials/ag/{ident}",
            json={"patch_ops": [{"op": "add", "path": f"/k{i}", "value": i}]},
        )

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        responses = list(pool.map(add_key, range(WRITERS)))

    for i, r in enumerate(responses):
        skip_if_throttled(r)
        # A 500 here is the sqlite "database is locked" failure mode; on Postgres
        # a contended writer must BLOCK on the row lock, not be rejected.
        assert r.status_code == 200, f"writer {i} → {r.status_code}: {r.text}"

    final = authed.get(f"/api/materials/ag/{ident}")
    assert final.status_code == 200, final.text
    doc = final.json()

    missing = [f"k{i}" for i in range(WRITERS) if f"k{i}" not in doc]
    assert not missing, (
        f"lost updates — these concurrent writes vanished: {missing}. "
        "The read-modify-write is not being serialized under the row lock."
    )
    # Content nobody touched must survive every concurrent merge.
    assert doc["name"] == {"ne": "नेपाल सरकार विरुद्ध राम बहादुर थापा"}
    assert doc["jawafdehi:sourceType"] == "AG_ABHIYOG_PATRA"


def test_the_etag_advances_under_concurrent_writes(authed, material):
    """Each accepted write must produce a new version token.

    A token that did not move after someone else's write would defeat If-Match:
    a stale client would be told its snapshot is current and clobber unseen work.
    """
    ident, iri = material
    before = authed.get(f"/api/materials/ag/{ident}").headers.get("ETag")
    assert before, "GET did not emit an ETag"

    def add_key(i: int):
        return authed.patch(
            f"/api/materials/ag/{ident}",
            json={"patch_ops": [{"op": "add", "path": f"/e{i}", "value": i}]},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(add_key, range(4)))

    after = authed.get(f"/api/materials/ag/{ident}").headers.get("ETag")
    assert after and after != before, (before, after)

    # And the now-stale token must be refused rather than silently accepted.
    stale = authed.patch(
        f"/api/materials/ag/{ident}",
        json={"patch_ops": [{"op": "add", "path": "/late", "value": 1}]},
        headers={"If-Match": before},
    )
    skip_if_throttled(stale)
    assert stale.status_code == 412, stale.status_code
