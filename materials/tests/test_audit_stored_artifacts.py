"""A material can advertise a file it does not have.

``jawafdehi:artifacts`` reports ``pdf.original: true`` on the strength of a RAW
link being present. Nothing checks the object behind it, and sampling 30
materials at random on 2026-10-28 found 2 whose stored object answers HTTP 200
with no bytes — so a reader following the link gets an empty file and no error.

Two halves: the upload boundary stops making new ones, and this command finds
the ones already stored.
"""

from __future__ import annotations

from io import StringIO
from unittest import mock

import pytest
from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from jawafdehi_shared.storage import store_file_as_link
from materials.jsonld import MATERIAL_CONTEXT
from materials.models import Material

OURS = "https://s3.jawafdehi.org/case_uploads/abc.pdf"
THEIRS = "https://ag.gov.np/storage/abhiyogPatra/x.pdf"


class EmptyUploadTests(TestCase):
    """An empty object is worse than no object: the link still lands."""

    def test_an_empty_file_is_refused(self):
        with pytest.raises(ValueError, match="empty file"):
            store_file_as_link(ContentFile(b"", name="material.pdf"), role="RAW")

    def test_a_file_with_bytes_is_stored(self):
        link = store_file_as_link(ContentFile(b"%PDF-1.4", name="material.pdf"))

        assert link["link"]
        assert link["role"] == "RAW"


def _material(ident: str, media: list[dict]) -> Material:
    iri = f"https://jawafdehi.org/material/probe/{ident}"
    return Material.objects.create(
        iri=iri,
        material_type="charge_sheet",
        source="probe",
        ident=ident,
        data={
            "@context": MATERIAL_CONTEXT,
            "@type": "DigitalDocument",
            "@id": iri,
            "associatedMedia": media,
            "jawafdehi:sourceUrl": THEIRS,
        },
    )


class _Response:
    def __init__(self, status: int, body: bytes):
        self.status_code = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size=1):
        if self._body:
            yield self._body


@override_settings(JAWAFDEHI_S3_BASE="https://s3.jawafdehi.org")
class AuditTests(TestCase):
    databases = "__all__"

    def _run(self, body: bytes = b"", status: int = 200, **kwargs) -> str:
        out, err = StringIO(), StringIO()
        with mock.patch(
            "requests.Session.get", return_value=_Response(status, body)
        ) as get:
            call_command(
                "audit_stored_artifacts", stdout=out, stderr=err, **kwargs
            )
        self.get = get
        return out.getvalue() + err.getvalue()

    def test_an_empty_object_is_reported_and_exits_one(self):
        _material("empty", [{"contentUrl": OURS, "jawafdehi:linkRole": "RAW"}])

        with pytest.raises(CommandError) as caught:
            self._run(body=b"")

        assert caught.value.returncode == 1

    def test_an_object_with_bytes_passes(self):
        _material("ok", [{"contentUrl": OURS, "jawafdehi:linkRole": "RAW"}])

        output = self._run(body=b"%PDF")

        assert "every stored artifact has bytes" in output

    def test_the_publishers_own_copy_is_not_our_problem(self):
        """``is_our_copy`` is where that line already lives."""
        _material("theirs", [{"contentUrl": THEIRS, "jawafdehi:linkRole": "RAW"}])

        output = self._run(body=b"")

        assert "checked 0 stored artifact(s)" in output
        self.get.assert_not_called()

    def test_the_repair_route_is_reported_per_document(self):
        """Re-fetchable and not-re-fetchable are different outcomes.

        ⚠️ The report is read from the captured streams, not from inside the
        `pytest.raises` block: an assert after the raising call never runs, which
        is how an earlier version of this test passed while checking nothing.
        """
        _material("empty2", [{"contentUrl": OURS, "jawafdehi:linkRole": "RAW"}])

        out, err = StringIO(), StringIO()
        with (
            mock.patch("requests.Session.get", return_value=_Response(200, b"")),
            pytest.raises(CommandError),
        ):
            call_command("audit_stored_artifacts", stdout=out, stderr=err)

        assert "can be re-fetched from the publisher" in out.getvalue()

    def test_an_empty_redacted_pdf_is_probed_too(self):
        """The artifact that REPLACES a withheld original is the one that matters.

        ⚠️ `REDACTED_PDF` specifically. An earlier version of this test used
        `REDACTED_MARKDOWN`, which `CHECKED_ROLES` already contained — so it
        passed against the unchanged set and gave the one line this commit
        actually adds no coverage at all. Remove `REDACTED_PDF_ROLE` from
        `CHECKED_ROLES` and this must fail.
        """
        _material(
            "redacted-empty",
            [{"contentUrl": OURS, "jawafdehi:linkRole": "REDACTED_PDF"}],
        )

        out, err = StringIO(), StringIO()
        with (
            mock.patch("requests.Session.get", return_value=_Response(200, b"")),
            pytest.raises(CommandError),
        ):
            call_command("audit_stored_artifacts", stdout=out, stderr=err)

        assert "checked 1 stored artifact(s)" in out.getvalue()
        assert "empty=1" in out.getvalue()
