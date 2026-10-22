"""Redaction happens where every writer passes, and the original survives.

Two requirements drive this, and they pull in opposite directions:

* everything public — the detail payload, the search body, the MCP tools that
  read through them — carries the REDACTED text;
* the text as it arrived is kept, because transcription is expensive and not
  reproducible byte-for-byte, so recovering an original must never mean OCR-ing
  a document a second time.

So the text is redacted once, at the single upsert primitive, and the original
is stored on the row outside ``data`` where only a permitted caller can reach
it.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_redaction_boundary.py
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework.test import APITestCase

from materials import search_index
from materials.jsonld import MATERIAL_CONTEXT
from materials.models import Material
from materials.single_source_ingest import upsert_single_source_material

SOURCE, IDENT = "charge_sheet", "ag-99001"
IRI = f"https://jawafdehi.org/material/{SOURCE}/{IDENT}"
URL = f"/api/materials/{SOURCE}/{IDENT}"

IDENTIFIER = "१२-०१-७५-०४५६७"
BODY = f"प्रतिवादीको नागरिकता नं. {IDENTIFIER} रहेको छ।"


def _doc(text=None) -> dict:
    doc = {
        "@context": MATERIAL_CONTEXT,
        "@type": "DigitalDocument",
        "@id": IRI,
        "name": {"ne": "अभियोगपत्र"},
    }
    if text is not None:
        doc["text"] = text
    return doc


class BoundaryTests(TestCase):
    databases = {"default", "ngm"}

    def test_a_scraper_ingest_is_redacted_without_the_scraper_doing_anything(self):
        # The whole point of the boundary. Four scrapers set data["text"]
        # directly and none of them calls a redactor; the AG shelf is charge
        # sheets naming individuals and ingests entirely that way, so it was the
        # highest-PII corpus we hold and the one still publishing identifiers.
        material = upsert_single_source_material(
            _doc({"ne": BODY}), material_type="document"
        )

        self.assertNotIn(IDENTIFIER, material.data["text"]["ne"])
        self.assertIn("[REDACTED:CITIZENSHIP-NO]", material.data["text"]["ne"])

    def test_the_search_body_cannot_carry_the_identifier(self):
        material = upsert_single_source_material(
            _doc({"ne": BODY}), material_type="document"
        )

        # build_doc reads data["text"] whoever wrote it, so redacting at the
        # boundary is what keeps an identifier out of a public search snippet.
        self.assertNotIn(IDENTIFIER, search_index.build_doc(material)["body"])

    def test_the_text_as_it_arrived_is_kept(self):
        material = upsert_single_source_material(
            _doc({"ne": BODY}), material_type="document"
        )

        # Not re-derivable: transcription is expensive and not byte-for-byte
        # reproducible, so an original we did not keep is an original that costs
        # an OCR run to answer a question about.
        self.assertIn(IDENTIFIER, material.source_text)

    def test_nothing_is_stored_twice_when_there_was_nothing_to_remove(self):
        material = upsert_single_source_material(
            _doc({"ne": "कुनै पहिचान नभएको सामान्य विवरण।"}),
            material_type="document",
        )

        # The overwhelming majority of the archive. A second copy of identical
        # bytes answers no question and doubles what a takedown has to reach.
        self.assertEqual(material.source_text, "")

    def test_a_re_ingest_that_loses_the_identifier_clears_the_original(self):
        upsert_single_source_material(_doc({"ne": BODY}), material_type="document")

        upsert_single_source_material(
            _doc({"ne": "सच्याइएको विवरण।"}), material_type="document"
        )

        # Otherwise the row goes on answering with text the document no longer
        # has — a correction that does not take.
        self.assertEqual(Material.objects.get(pk=IRI).source_text, "")

    def test_a_document_with_no_text_is_untouched(self):
        material = upsert_single_source_material(_doc(), material_type="document")

        self.assertNotIn("text", material.data)
        self.assertEqual(material.source_text, "")

    def test_a_bare_string_text_is_redacted_too(self):
        # The corpus carries both shapes; assuming the language map drops the
        # redaction silently on whichever documents use the other one.
        material = upsert_single_source_material(
            _doc(BODY), material_type="document"
        )

        self.assertNotIn(IDENTIFIER, material.data["text"])


class WhoSeesTheOriginalTests(APITestCase):
    databases = {"default", "ngm"}

    def setUp(self):
        upsert_single_source_material(_doc({"ne": BODY}), material_type="document")

    def test_a_signed_out_reader_gets_the_redacted_text(self):
        response = self.client.get(URL)

        self.assertNotIn(IDENTIFIER, response.data["text"]["ne"])

    def test_a_caseworker_gets_the_redacted_text(self):
        user = get_user_model().objects.create_user("cw", password="x")
        user.groups.add(Group.objects.get_or_create(name="Caseworker")[0])
        self.client.force_authenticate(user)

        response = self.client.get(URL)

        # Authoring and moderating cases needs the document, not the
        # identifiers printed in it.
        self.assertNotIn(IDENTIFIER, response.data["text"]["ne"])

    def test_the_permission_gets_the_text_as_it_arrived(self):
        group = Group.objects.create(name="PrivacyReviewer")
        group.permissions.add(
            Permission.objects.get(
                codename="view_unredacted",
                content_type=ContentType.objects.get_for_model(Material),
            )
        )
        user = get_user_model().objects.create_user("reviewer", password="x")
        user.groups.add(group)
        self.client.force_authenticate(user)

        response = self.client.get(URL)

        # Same URL, no second transcription, no separate route to know about.
        self.assertIn(IDENTIFIER, response.data["text"]["ne"])

    def test_one_url_two_representations_so_the_response_varies(self):
        response = self.client.get(URL)

        self.assertIn("Authorization", response.get("Vary", ""))


class ConvertPathTests(TestCase):
    """The convert job writes the row directly, so it needs the same guarantees.

    It does NOT go through upsert_single_source_material — it does a locked
    read-modify-write — so the ingest boundary does not cover it. That is also
    the path doing the expensive half of the work, which makes it the one place
    an unkept original costs an OCR run to recover.
    """

    databases = {"default", "ngm"}

    def _apply(self, text: str):
        from unittest.mock import patch

        from materials.conversion import apply_convert_result

        upsert_single_source_material(_doc(), material_type="document")

        class _Job:
            pk = 1
            payload = {"material_iri": IRI}
            result = {"text": text}

        links = iter(
            [
                {"link": "https://cdn/a.md", "role": "MARKDOWN"},
                {"link": "https://cdn/a.red.md", "role": "REDACTED_MARKDOWN"},
            ]
        )
        with patch(
            "jawafdehi_shared.storage.store_file_as_link",
            side_effect=lambda *a, **k: next(links),
        ):
            apply_convert_result(_Job(), {"text": text, "source_url": "https://a/x.pdf"})
        return Material.objects.get(pk=IRI)

    def test_the_converted_text_is_redacted_and_the_original_kept(self):
        row = self._apply(BODY)

        self.assertNotIn(IDENTIFIER, row.data["text"]["ne"])
        self.assertIn(IDENTIFIER, row.source_text)

    def test_likhit_markers_do_not_reach_the_indexed_text(self):
        row = self._apply(
            "<!-- likhit:needs-ocr pages=3 reason=x -->\n"
            "<!-- likhit:page 1 -->\nपहिलो पृष्ठ\n<!-- likhit:page 2 -->\nदोस्रो पृष्ठ"
        )

        # This is the field search reads: a 446-page report would otherwise
        # contribute 446 HTML comments to its body, and a highlight can return
        # one inside a user-facing snippet.
        body = row.data["text"]["ne"]
        self.assertNotIn("likhit:page", body)
        self.assertNotIn("needs-ocr", body)
        self.assertIn("पहिलो पृष्ठ", body)

    def test_both_transcripts_are_attached_with_honest_provenance(self):
        row = self._apply(BODY)

        by_role = {
            m["jawafdehi:linkRole"]: m
            for m in row.data["associatedMedia"]
            if isinstance(m, dict)
        }
        self.assertIn("MARKDOWN", by_role)
        self.assertIn("REDACTED_MARKDOWN", by_role)
        # The redacted copy was produced by redacting our transcript, not by
        # OCR-ing the publisher's file. Stamping both "ocr" would describe two
        # artifacts as the same claim about the same source.
        self.assertEqual(
            by_role["REDACTED_MARKDOWN"]["jawafdehi:provenance"]["fetch_method"],
            "redaction",
        )
        self.assertEqual(
            by_role["MARKDOWN"]["jawafdehi:provenance"]["fetch_method"], "ocr"
        )

    def test_a_redaction_failure_stores_nothing_rather_than_the_transcript(self):
        from unittest.mock import patch

        with patch(
            "materials.redaction.redact_text", side_effect=RuntimeError("regex blew up")
        ):
            row = self._apply(BODY)

        # Any failure, not just a missing redactor. Storing the conversion
        # anyway would publish the identifiers; storing half of it would label a
        # document redacted on the strength of a failure.
        self.assertNotIn("text", row.data)
        self.assertEqual(row.source_text, "")
