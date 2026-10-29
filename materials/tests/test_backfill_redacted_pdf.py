"""The redacted PDF had no producer, and the artifact model published one anyway.

``materials.artifacts`` has described four artifacts since #542 and publishes the
redacted two. Nothing ever wrote ``REDACTED_PDF``, so ``is_withheld`` went on
publishing every original — which for a scanned document is the entire exposure,
since redacting the text removes the value from search and leaves it legible in
the file.

What is pinned here is the orchestration: which documents are picked, what
happens to each outcome likhit can return, and that a dry run writes nothing.
The redaction itself is likhit's to test, and ``redact_pdf`` is mocked
throughout — including its refusals, which are the behaviour that matters most
here.
"""

from __future__ import annotations

from io import StringIO
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from materials import artifacts
from materials.jsonld import MATERIAL_CONTEXT
from materials.models import Material

OURS = "https://s3.jawafdehi.org/case_uploads/abc.pdf"
THEIRS = "https://ag.gov.np/storage/abhiyogPatra/x.pdf"


class _Report:
    values_removed = 3


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
        },
    )


def _raw(url: str = OURS) -> dict:
    return {"contentUrl": url, "jawafdehi:linkRole": "RAW"}


def _raw_for(ident: str) -> dict:
    """A distinct stored URL per material, so a fetch mock can tell them apart."""
    return {
        "contentUrl": f"https://s3.jawafdehi.org/case_uploads/{ident[0]}.pdf",
        "jawafdehi:linkRole": "RAW",
    }


@override_settings(JAWAFDEHI_S3_BASE="https://s3.jawafdehi.org")
class BackfillRedactedPdfTests(TestCase):
    databases = "__all__"

    def _run(self, *args, redact=None, fetched=b"%PDF-1.4 body", **kwargs):
        out, err = StringIO(), StringIO()
        redactor = redact if redact is not None else mock.DEFAULT
        with (
            mock.patch(
                "materials.extract.fetch_document", return_value=fetched
            ) as fetch,
            mock.patch("likhit.privacy.pdf.redact_pdf") as redact_pdf,
        ):
            if redact is not None:
                redact_pdf.side_effect = redactor
            else:
                redact_pdf.side_effect = lambda src, dst, **kw: (
                    dst.write_bytes(b"%PDF redacted"),
                    _Report(),
                )[1]
            call_command(
                "backfill_redacted_pdf", *args, stdout=out, stderr=err, **kwargs
            )
        self.fetch = fetch
        return out.getvalue() + err.getvalue()

    def _roles(self, material: Material) -> list[str]:
        material.refresh_from_db()
        return [
            artifacts.role_of(mo) for mo in artifacts.media_list(material.data or {})
        ]

    def test_a_redacted_pdf_is_published_and_the_original_kept(self):
        material = _material("p1", [_raw()])

        output = self._run("--apply")

        assert artifacts.REDACTED_PDF_ROLE in self._roles(material)
        assert "RAW" in self._roles(material), "the original must survive, withheld"
        assert "published=1" in output

    def test_a_dry_run_writes_nothing(self):
        material = _material("p2", [_raw()])

        output = self._run()

        assert artifacts.REDACTED_PDF_ROLE not in self._roles(material)
        assert "would_publish=1" in output

    def test_a_document_that_already_has_one_is_skipped(self):
        material = _material(
            "p3",
            [
                _raw(),
                {
                    "contentUrl": "https://s3.jawafdehi.org/case_uploads/r.pdf",
                    "jawafdehi:linkRole": artifacts.REDACTED_PDF_ROLE,
                },
            ],
        )

        output = self._run("--apply")

        self.fetch.assert_not_called()
        assert self._roles(material).count(artifacts.REDACTED_PDF_ROLE) == 1
        assert "already_published=1" in output

    def test_the_publishers_own_copy_is_not_redacted(self):
        """We did not store that file; a derivative of it is a new publication."""
        _material("p4", [_raw(THEIRS)])

        output = self._run("--apply")

        self.fetch.assert_not_called()
        assert "no_pdf=1" in output

    def test_a_page_class_likhit_cannot_handle_is_counted_not_published(self):
        material = _material("p5", [_raw()])

        def blocked(src, dst, **kw):
            raise ValueError("page 3 is scanned and cannot be redacted")

        with pytest.raises(CommandError) as caught:
            self._run("--apply", redact=blocked)

        assert artifacts.REDACTED_PDF_ROLE not in self._roles(material)
        assert caught.value.returncode == 1

    def test_residue_in_its_own_output_is_a_refusal_not_a_blocked_page(self):
        """Two different failures reach us through one exception type.

        ⚠️ Read from the captured STDOUT, never from the CommandError. That
        message names all three counts unconditionally ("… N were refused by
        verification …"), so asserting `"refused" in str(exc)` passes for a
        blocked document too — which is how two earlier versions of this test
        passed while checking nothing. Invert the classifier in `_one` and this
        assertion must fail.
        """
        material = _material("p6", [_raw()])

        def unverified(src, dst, **kw):
            raise ValueError("verification failed: 1 value(s) of kind ['citizenship']")

        out, err = StringIO(), StringIO()
        with (
            mock.patch("materials.extract.fetch_document", return_value=b"%PDF"),
            mock.patch("likhit.privacy.pdf.redact_pdf", side_effect=unverified),
            pytest.raises(CommandError),
        ):
            call_command(
                "backfill_redacted_pdf", "--apply", stdout=out, stderr=err
            )

        report = out.getvalue()
        assert "refused=1" in report, report
        assert "blocked=1" not in report, report
        assert artifacts.REDACTED_PDF_ROLE not in self._roles(material)

    def test_a_body_that_is_not_a_pdf_does_not_end_the_run(self):
        """PyMuPDF raises FileDataError, which is a RuntimeError, not a ValueError."""
        material = _material("p8", [_raw()])

        def not_a_pdf(src, dst, **kw):
            raise RuntimeError("cannot open broken document")

        with pytest.raises(CommandError) as caught:
            self._run("--apply", redact=not_a_pdf)

        assert caught.value.returncode == 1
        assert artifacts.REDACTED_PDF_ROLE not in self._roles(material)

    def test_a_stored_pdf_whose_key_lacks_an_extension_is_still_read(self):
        """An object key's suffix comes from the client-supplied filename."""
        material = _material(
            "p9",
            [
                {
                    "contentUrl": "https://s3.jawafdehi.org/case_uploads/abc",
                    "jawafdehi:linkRole": "RAW",
                }
            ],
        )

        self._run("--apply")

        assert artifacts.REDACTED_PDF_ROLE in self._roles(material)

    def test_an_empty_stored_object_is_reported(self):
        material = _material("p7", [_raw()])

        with pytest.raises(CommandError):
            self._run("--apply", fetched=b"")

        assert artifacts.REDACTED_PDF_ROLE not in self._roles(material)


@override_settings(JAWAFDEHI_S3_BASE="https://s3.jawafdehi.org")
class LimitCountsWorkDoneTests(TestCase):
    """🛑 A permanently-blocked document must not consume the batch budget.

    The command's own measurement says 18 of 28 charge sheets are blocked for
    good — 11 scanned, 7 carrying legacy pages. If those count toward `--limit`,
    a batched sweep spends its budget on them, and because they are skipped by
    nothing on the next run it spends the same budget on the same documents
    forever, re-downloading and re-redacting each one. That is the stall the
    limit was changed to fix, reached from the other side.
    """

    databases = "__all__"

    def test_blocked_documents_do_not_eat_the_budget(self):
        for n in ("a-blocked", "b-blocked", "c-ok"):
            _material(n, [_raw_for(n)])

        def selective(src, dst, **kw):
            if b"ok" in src.read_bytes():
                dst.write_bytes(b"%PDF redacted")
                return _Report()
            raise ValueError("page 2 is scanned and cannot be redacted")

        out, err = StringIO(), StringIO()
        with (
            mock.patch(
                "materials.extract.fetch_document",
                side_effect=lambda url, **kw: b"%PDF ok" if url.endswith("c.pdf")
                else b"%PDF blocked",
            ),
            mock.patch("likhit.privacy.pdf.redact_pdf", side_effect=selective),
            pytest.raises(CommandError),
        ):
            call_command(
                "backfill_redacted_pdf", "--apply", "--limit", "1",
                stdout=out, stderr=err,
            )

        # The two blocked documents come first alphabetically. If they counted,
        # the run would stop before ever reaching the one it can publish.
        assert "published=1" in out.getvalue(), out.getvalue()
