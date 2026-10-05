"""``GET /api/materials/`` omits the document body; the detail endpoint keeps it.

The list renders whole stored JSON-LD documents, so a page of 50 shipped
**41.5 MB uncompressed / 7.2 MB zstd**, of which the ``text`` body was 99.6%
(measured against production 2026-10-05; the heaviest single row, an Auditor
General report, was 1.89 MB on its own). Nothing consumes it there — the admin
table renders Name/Type/@id — and it cost ~6s of TTFB per call in detoast,
serialization and compression.

What these pin:

* the body is gone from the list, and its absence is FLAGGED rather than silent;
* the detail endpoint is untouched, so nothing leaves the open dataset;
* the stored ``data`` dict is not mutated on the way out — the anon branch used
  to hand back ``row.data`` itself, so popping without copying would corrupt the
  in-memory instance;
* a size ceiling per field, which is the part that survives us. ``text`` is the
  only unbounded field today, so the fix names it; the guard is what catches the
  next one (a transcript, OCR blocks) in CI instead of in production.
"""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import status
from rest_framework.test import APITestCase

from materials.jsonld import MATERIAL_CONTEXT
from materials.models import Material, Visibility

User = get_user_model()

# Comfortably above any real metadata field (the largest measured in production
# is associatedMedia at ~2 KB) and far below a body (1.89 MB).
MAX_FIELD_BYTES = 64 * 1024

BODY = "अख्तियार दुरुपयोग अनुसन्धान आयोग " * 4000


def _doc(iri: str, *, text: str | None = BODY) -> dict:
    doc = {
        "@context": MATERIAL_CONTEXT,
        "@type": "Legislation",
        "@id": iri,
        "name": {"ne": "ऐन"},
    }
    if text is not None:
        doc["text"] = text
    return doc


def _seed(source: str, ident: str, *, text: str | None = BODY) -> str:
    iri = f"https://jawafdehi.org/material/{source}/{ident}"
    Material.objects.create(
        iri=iri,
        material_type="legal_corpus",
        source=source,
        ident=ident,
        data=_doc(iri, text=text),
        visibility=Visibility.LISTED,
    )
    return iri


class ListAbridgesTextTests(APITestCase):
    databases = "__all__"

    def test_list_omits_the_body_and_says_so(self):
        _seed("nkp", "2080-act-1")
        row = self.client.get("/api/materials/").data["results"][0]

        assert "text" not in row
        assert row["jawafdehi:textOmitted"] is True
        # The metadata a consumer actually lists on is still all there.
        assert row["name"] == {"ne": "ऐन"}
        assert row["@type"] == "Legislation"

    def test_the_id_is_present_so_the_full_document_is_one_fetch_away(self):
        iri = _seed("nkp", "2080-act-1")
        row = self.client.get("/api/materials/").data["results"][0]
        assert row["@id"] == iri

    def test_a_document_with_no_body_is_not_flagged(self):
        """Otherwise the flag would mean nothing — it has to distinguish
        "withheld" from "there is none", which is its whole reason to exist."""
        _seed("nkp", "2080-act-1", text=None)
        row = self.client.get("/api/materials/").data["results"][0]

        assert "text" not in row
        assert "jawafdehi:textOmitted" not in row

    def test_detail_still_returns_the_whole_document(self):
        _seed("nkp", "2080-act-1")
        resp = self.client.get("/api/materials/nkp/2080-act-1")

        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["text"] == BODY
        assert "jawafdehi:textOmitted" not in resp.data

    def test_the_stored_document_is_not_mutated(self):
        """The anon branch used to return ``row.data`` — the live model attribute
        — so popping from it would strip the body from the instance, not the
        response. Nothing downstream in the request would get it back."""
        iri = _seed("nkp", "2080-act-1")
        self.client.get("/api/materials/")

        assert Material.objects.get(pk=iri).data["text"] == BODY

    def test_no_field_in_a_list_row_exceeds_the_ceiling(self):
        """The guard that outlives this change.

        ``text`` is named explicitly because it is the only unbounded field
        today. If someone later adds a transcript or OCR-block field to the
        stored document, this fails in CI and the fix is one line — rather than
        the endpoint quietly going back to multi-megabyte pages.
        """
        _seed("nkp", "2080-act-1")
        _seed("oag", "report-1")
        rows = self.client.get("/api/materials/").data["results"]

        assert rows, "fixture should list materials"
        for row in rows:
            for key, value in row.items():
                size = len(json.dumps(value, ensure_ascii=False).encode())
                assert size <= MAX_FIELD_BYTES, (
                    f"{key} is {size} bytes on {row.get('@id')}; a list row must "
                    f"stay metadata-sized. Add the field to _ABRIDGED_FIELD(S) in "
                    f"materials/views.py, or justify raising the ceiling."
                )


class AuthedListAbridgesTextTests(APITestCase):
    """The admin table is the heaviest consumer, and it was never the exception."""

    databases = "__all__"

    @classmethod
    def setUpTestData(cls):
        group, _ = Group.objects.get_or_create(name="Caseworker")
        cls.user = User.objects.create(username="oidc-caseworker")
        cls.user.groups.add(group)

    def test_authed_rows_are_abridged_but_keep_their_visibility_fields(self):
        _seed("nkp", "2080-act-1")
        self.client.force_authenticate(user=self.user)
        row = self.client.get("/api/materials/").data["results"][0]

        assert "text" not in row
        assert row["jawafdehi:textOmitted"] is True
        # The two fields this branch exists to add are still added.
        assert row["jawafdehi:visibility"] == Visibility.LISTED
        assert "jawafdehi:visibilityPolicy" in row
