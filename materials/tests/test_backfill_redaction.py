"""The backfill walks the archive without being able to destroy it.

What is pinned here is the safety argument rather than the redactor — that is
``test_redaction.py``'s job. These tests say: a dry run writes nothing, a stored
original is never overwritten, a document whose redaction came out incomplete is
refused instead of published, and a re-run is a no-op rather than a second pass
over the command's own output.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_backfill_redaction.py
"""

from __future__ import annotations

from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from materials import redaction
from materials.jsonld import MATERIAL_CONTEXT
from materials.models import Material

CITIZENSHIP_VALUE = "१२-०१-७५-०४५६७"
WITH_CITIZENSHIP = f"नागरिकता नं. {CITIZENSHIP_VALUE} रहेको व्यक्ति"
MOBILE_VALUE = "९८४१२३४५६७"
WITH_MOBILE = f"सम्पर्क {MOBILE_VALUE}"


def _material(ident: str, text, *, material_type="charge_sheet", **extra) -> Material:
    """A material carrying ``text``, written the way the archive already holds it.

    Deliberately ``objects.create`` rather than the upsert primitive: the whole
    population this command exists for is rows that never went through the
    primitive, because it did not redact when they were written.
    """
    iri = f"https://jawafdehi.org/material/probe/{ident}"
    return Material.objects.create(
        iri=iri,
        material_type=material_type,
        source="probe",
        ident=ident,
        data={
            "@context": MATERIAL_CONTEXT,
            "@type": "DigitalDocument",
            "@id": iri,
            "name": {"ne": "परीक्षण"},
            "text": text,
        },
        **extra,
    )


def _run(*args, **kwargs):
    out, err = StringIO(), StringIO()
    call_command("backfill_redaction", *args, stdout=out, stderr=err, **kwargs)
    return out.getvalue() + err.getvalue()


class DryRunTests(TestCase):
    databases = "__all__"

    def test_a_dry_run_changes_nothing(self):
        material = _material("dry", WITH_CITIZENSHIP)

        output = _run("--material-type", "charge_sheet")

        material.refresh_from_db()
        self.assertEqual(material.data["text"], WITH_CITIZENSHIP)
        self.assertEqual(material.source_text, "")
        self.assertIn("would_redact=1", output)
        self.assertIn("dry run", output)

    def test_a_document_with_nothing_to_remove_is_reported_clean(self):
        _material("clean", "यो कागजातमा व्यक्तिगत विवरण छैन।")

        output = _run("--material-type", "charge_sheet")

        self.assertIn("clean=1", output)


class ApplyTests(TestCase):
    databases = "__all__"

    def test_apply_redacts_the_text_and_keeps_the_original(self):
        material = _material("apply", WITH_CITIZENSHIP)

        _run("--material-type", "charge_sheet", "--apply")

        material.refresh_from_db()
        self.assertIn("[REDACTED:CITIZENSHIP-NO]", material.data["text"])
        self.assertNotIn(CITIZENSHIP_VALUE, material.data["text"])
        # The original is preserved rather than re-derivable: re-transcribing a
        # document is an OCR run and is not byte-for-byte reproducible.
        self.assertIn(CITIZENSHIP_VALUE, material.source_text)

    def test_a_language_map_is_redacted_in_every_language(self):
        material = _material("langmap", {"ne": WITH_CITIZENSHIP, "en": WITH_MOBILE})

        _run("--material-type", "charge_sheet", "--apply")

        material.refresh_from_db()
        self.assertNotIn(CITIZENSHIP_VALUE, material.data["text"]["ne"])
        self.assertNotIn(MOBILE_VALUE, material.data["text"]["en"])

    def test_running_twice_is_a_no_op_and_keeps_the_first_original(self):
        material = _material("twice", WITH_CITIZENSHIP)

        _run("--material-type", "charge_sheet", "--apply")
        material.refresh_from_db()
        first_original = material.source_text
        output = _run("--material-type", "charge_sheet", "--apply")

        material.refresh_from_db()
        self.assertEqual(material.source_text, first_original)
        self.assertIn(CITIZENSHIP_VALUE, material.source_text)
        self.assertIn("clean=1", output)

    def test_a_stored_original_is_never_overwritten(self):
        """The one irreversible mistake available to this command.

        A row that already carries an original holds text that is itself a
        redacted derivative. Writing that over ``source_text`` would replace the
        pristine copy with a partly-redacted one — and nothing could get it back.
        """
        pristine = '"नागरिकता नं. ९९-९९-९९-९९९९९ र सम्पर्क ९८००००००००"'
        material = _material("stored", WITH_MOBILE, source_text=pristine)

        _run("--material-type", "charge_sheet", "--apply")

        material.refresh_from_db()
        self.assertEqual(material.source_text, pristine)
        # …and the text was still redacted, which is the half that makes keeping
        # the original safe rather than an excuse to skip the document.
        self.assertNotIn(MOBILE_VALUE, material.data["text"])

    def test_a_soft_deleted_row_is_left_alone(self):
        material = _material("deleted", WITH_CITIZENSHIP, is_deleted=True)

        _run("--apply")

        material.refresh_from_db()
        self.assertEqual(material.data["text"], WITH_CITIZENSHIP)


class GateTests(TestCase):
    databases = "__all__"

    def test_residual_personal_data_refuses_the_document(self):
        """A gate that cannot be made to fail is decoration.

        The scanner is neutralised into always reporting a survivor, which is what
        an incomplete redaction looks like from here: the document must be left
        exactly as it was, and the run must not exit 0.
        """
        material = _material("refuse", WITH_CITIZENSHIP)

        with mock.patch(
            "likhit.privacy.scan_text",
            return_value=({"citizenship": 1}, {}),
        ):
            with self.assertRaises(CommandError) as caught:
                _run("--material-type", "charge_sheet", "--apply")

        self.assertEqual(caught.exception.returncode, 2)
        material.refresh_from_db()
        self.assertEqual(material.data["text"], WITH_CITIZENSHIP)
        self.assertEqual(material.source_text, "")

    def test_the_gate_knows_every_class_the_scanner_reports(self):
        """An unknown class is an ungated one, so upstream adding one must fail here.

        ``_removal_tiers`` records which tier first removes each class. If likhit
        starts reporting a class that table does not know, the gate would silently
        stop checking it — the one failure mode a gate must not have.
        """
        from likhit.privacy import scan_text

        detections, _names = scan_text(f"{WITH_CITIZENSHIP} {WITH_MOBILE}")
        unknown = (
            set(detections) - set(redaction._removal_tiers()) - redaction.NOT_REMOVED
        )

        self.assertEqual(unknown, set(), f"likhit reports ungated class(es): {unknown}")

    def test_nothing_exempt_from_the_gate_is_ever_actually_removed(self):
        """The test above passes if a REMOVABLE class is wrongly listed as exempt.

        That is not hypothetical — it is the defect the first corpus run found.
        ``phone_labelled`` was read as a shape diagnostic and exempted, and the
        coverage test above was satisfied *by the exemption*. likhit removes it:
        an English-labelled phone number is journalled under that kind, 193 times
        in one charge-sheet sweep, so residual ones were going ungated.

        So assert the exemption from the other side: anything we refuse to gate on
        must be something the redactor never takes out.
        """
        from likhit.privacy import RedactionMode
        from likhit.privacy import redact_text as likhit_redact

        probe = (
            f"{WITH_CITIZENSHIP} {WITH_MOBILE} Phone No.: 014234567 "
            "इमेल: ram@example.com.np खाता नं. ०१२३४५६७८९०१२३ "
            "स्थायी लेखा नम्बर (PAN): ६०१२३४५६७"
        )
        removed = {
            row["kind"]
            for row in likhit_redact(probe, mode=RedactionMode.STRICT).journal
        }

        wrongly_exempt = removed & redaction.NOT_REMOVED
        self.assertEqual(
            wrongly_exempt,
            set(),
            f"the gate exempts {wrongly_exempt}, which the redactor does remove",
        )


class SelectionTests(TestCase):
    databases = "__all__"

    def test_the_most_sensitive_types_are_walked_first(self):
        """An aborted run should have done the documents that matter most."""
        _material("news", WITH_CITIZENSHIP, material_type="news")
        _material("sheet", WITH_CITIZENSHIP, material_type="charge_sheet")

        output = _run("--limit", "1", "--apply")

        self.assertIn("charge_sheet", output)
        self.assertEqual(
            Material.objects.get(ident="news").data["text"], WITH_CITIZENSHIP
        )

    def test_a_single_document_can_be_named(self):
        named = _material("named", WITH_CITIZENSHIP)
        other = _material("other", WITH_CITIZENSHIP)

        _run("--iri", named.iri, "--apply")

        other.refresh_from_db()
        self.assertEqual(other.data["text"], WITH_CITIZENSHIP)
        named.refresh_from_db()
        self.assertNotIn(CITIZENSHIP_VALUE, named.data["text"])


class WithheldVisibilityTests(TestCase):
    """A withheld transcript must be countable, or the gate is a silent drop.

    It is deliberately not announced in the public payload — see
    ``redaction.withheld_by_the_write_path`` for why that would be a map to
    unredacted data while the original PDFs are still served. So the operator
    signal has to be exact, and it is: an original kept outside ``data`` while
    ``data`` serves no text is a combination nothing else produces.
    """

    databases = "__all__"

    def test_a_withheld_row_is_counted_and_a_normal_one_is_not(self):
        withheld = _material("withheld", WITH_CITIZENSHIP, source_text='"original"')
        withheld.data.pop("text")
        withheld.save()
        _material("ordinary", WITH_CITIZENSHIP)
        _material("redacted-normally", "कुनै पाठ", source_text='"original"')

        found = list(redaction.withheld_by_the_write_path())

        self.assertEqual([m.iri for m in found], [withheld.iri])

    def test_the_count_is_reported_even_when_nothing_else_is(self):
        withheld = _material("withheld2", WITH_CITIZENSHIP, source_text='"original"')
        withheld.data.pop("text")
        withheld.save()

        output = _run()

        self.assertIn("1 document(s) are carrying a withheld transcript", output)
