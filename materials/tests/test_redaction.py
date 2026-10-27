"""Personal data is removed before anything stores the text.

The pipeline's contract with the redaction plan: unredacted text exists only in
the extracting process's memory. What is pinned here is that contract, which tier
we ask likhit for (and so what a published transcript still contains), and the one
interaction that would otherwise go unnoticed — a removal reported as a failed
decode.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_redaction.py
"""

from __future__ import annotations

from collections import Counter
from unittest import TestCase, mock

from django.test import override_settings

from materials.redaction import (
    REDACTED_SUFFIX,
    RedactionIncomplete,
    RedactionUnavailable,
    redact_bundle,
    redact_jsonld,
    redact_text,
)
from materials.extraction.bundle import ContentBundle, PageRecord, TableRecord
from materials.extraction.pdf import _devanagari_share, _fidelity

CITIZENSHIP = "नागरिकता नं. १२-०१-७५-०४५६७ रहेको"
DATE_OF_BIRTH = "जन्म मिति २०४५।०३।१२ रहेको"
MOBILE = "सम्पर्क ९८४१२३४५६७"
EMAIL = "इमेल: ram.bahadur@example.com.np"
BANK = "बैंक खाता नं. ०१२३४५६७८९०१२३"
PAN = "स्थायी लेखा नम्बर (PAN): ६०१२३४५६७"


def _bundle(**kwargs) -> ContentBundle:
    defaults = dict(
        doc_id="oag-11133",
        dataset="jawafdehi:pdf-extract/v1",
        text_source="likhit",
        page_count=1,
        pages=[PageRecord(page_no=1, text=f"{CITIZENSHIP}। {DATE_OF_BIRTH}।", chars=0)],
        tables=[],
    )
    defaults.update(kwargs)
    return ContentBundle(**defaults)



class RedactTextTests(TestCase):
    """What the redactor removes, and — just as load-bearing — what it does not."""

    def test_a_citizenship_number_and_a_date_of_birth_are_removed(self):
        redacted, counts = redact_text(f"{CITIZENSHIP}। {DATE_OF_BIRTH}।")

        self.assertIn("[REDACTED:CITIZENSHIP-NO]", redacted)
        self.assertIn("[REDACTED:DATE-OF-BIRTH]", redacted)
        self.assertNotIn("१२-०१-७५-०४५६७", redacted)
        self.assertEqual(counts["citizenship"], 1)

    def test_contact_details_and_a_bank_account_are_removed_too(self):
        # The tier we ship is `strict`, not the narrower adjudicated
        # `identifiers`. On a real charge sheet the difference is 3 removals
        # against 11, and the eight it adds are mostly phone numbers — so a test
        # that only pinned citizenship would pass at either tier and tell us
        # nothing about which one is live.
        redacted, counts = redact_text(f"{MOBILE}। {EMAIL}। {BANK}।")

        self.assertNotIn("९८४१२३४५६७", redacted)
        self.assertNotIn("ram.bahadur@example.com.np", redacted)
        self.assertNotIn("०१२३४५६७८९०१२३", redacted)
        self.assertEqual(
            {"mobile", "email", "bank_account"}, set(counts) & {"mobile", "email", "bank_account"}
        )

    def test_a_pan_survives_because_it_identifies_a_company(self):
        # Not an oversight and not a tier that can be widened to cover it: a PAN
        # is a business registration number, public by design, and often the
        # only handle tying a procurement finding to a registered contractor.
        redacted, _counts = redact_text(PAN)

        self.assertIn("६०१२३४५६७", redacted)

    def test_the_counter_is_keyed_on_what_was_removed(self):
        # likhit's raw statistics carry `*_targeted` for the contact classes,
        # `*_redacted` for the others, and diagnostics like `physical_cells` on
        # every call. Keying on them made "did we redact anything?" true for
        # every document, and would have hidden every contact removal.
        _redacted, counts = redact_text("कुनै पनि व्यक्तिगत विवरण नभएको पाठ।")

        self.assertEqual(counts, Counter())

    @override_settings(MATERIALS_REDACTION_MODE="identifiers-ish")
    def test_an_unreadable_tier_refuses_rather_than_falling_back(self):
        # A fallback would mean nobody knows what scope the corpus was redacted
        # at, and would write that uncertainty into every document from here.
        with self.assertRaises(RedactionUnavailable):
            redact_text(CITIZENSHIP)

    @override_settings(MATERIALS_REDACTION_MODE="none")
    def test_the_do_nothing_tier_is_refused(self):
        # `none` is a legitimate likhit tier — it is how the research corpus and
        # the retained original are produced. Reaching it through THIS module
        # would publish the text unredacted.
        with self.assertRaises(RedactionUnavailable):
            redact_text(CITIZENSHIP)

    def test_a_value_in_a_cell_away_from_its_label_is_found(self):
        markdown = (
            "| नाम | नागरिकता नं. |\n| --- | --- |\n"
            "| राम बहादुर | १२-०१-७५-०४५६७ |"
        )

        redacted, _counts = redact_text(markdown)

        # The whole reason both passes run: the inline pass cannot see a value
        # that sits away from its label.
        self.assertIn("[REDACTED:TABLE-CITIZENSHIP-NO]", redacted)

    def test_a_bare_value_with_no_label_anywhere_is_not_reachable(self):
        # Neither pass can touch this, and that is the redactor's precision
        # guard rather than a gap: a digit run on its own is also a case
        # number, an amount, or a clause reference.
        redacted, _counts = redact_text("१२-०१-७५-०४५६७")

        self.assertEqual(redacted, "१२-०१-७५-०४५६७")

    def test_redacting_twice_changes_nothing_the_second_time(self):
        once, _ = redact_text(CITIZENSHIP)
        twice, _ = redact_text(once)

        # What keeps a re-conversion of an unchanged document byte-identical.
        self.assertEqual(once, twice)

    def test_empty_text_is_left_alone(self):
        self.assertEqual(redact_text("   "), ("   ", Counter()))


class RedactBundleTests(TestCase):
    def test_a_citizenship_number_and_a_date_of_birth_do_not_reach_the_writer(self):
        bundle = _bundle()

        totals = redact_bundle(bundle)

        text = bundle.pages[0].text
        self.assertIn("[REDACTED:CITIZENSHIP-NO]", text)
        self.assertIn("[REDACTED:DATE-OF-BIRTH]", text)
        self.assertNotIn("१२-०१-७५-०४५६७", text)
        self.assertNotIn("२०४५।०३।१२", text)
        self.assertEqual(totals["citizenship"], 1)
        self.assertEqual(totals["date_of_birth"], 1)

    def test_a_mobile_number_in_a_bundle_is_removed_too(self):
        """This assertion used to run the other way, and the inversion is the
        point of the tier change rather than a test being fixed to pass.

        While the bundle pass drove likhit's inline and table passes by hand, a
        mobile number survived every one of them: `[REDACTED:PHONE-NO]` was a
        registered placeholder that nothing we called emitted. Asking
        `likhit.privacy.redact_text` for the `strict` tier is what closed that,
        and on the charge-sheet corpus phone numbers are the single largest
        class of personal data — so a bundle test that still pinned the gap
        would now be pinning a defect.
        """

        bundle = _bundle(pages=[PageRecord(page_no=1, text=MOBILE, chars=0)])

        totals = redact_bundle(bundle)

        self.assertIn("[REDACTED:PHONE-NO]", bundle.pages[0].text)
        self.assertNotIn("९८४१२३४५६७", bundle.pages[0].text)
        self.assertEqual(totals["mobile"], 1)

    def test_the_document_records_that_it_was_redacted(self):
        bundle = _bundle()

        redact_bundle(bundle)

        # A reader comparing our text against the published PDF has to be able to
        # tell a removal from a transcription error. The placeholders say so on
        # the pages that carried a value; this says so for the document.
        self.assertTrue(bundle.text_source.endswith(REDACTED_SUFFIX))

    def test_redacting_twice_does_not_stack_the_marker(self):
        bundle = _bundle()

        redact_bundle(bundle)
        redact_bundle(bundle)

        self.assertEqual(bundle.text_source, f"likhit{REDACTED_SUFFIX}")
        # And the text is unchanged the second time round, which is what keeps
        # the content ETag stable across a re-extract of an unchanged document.
        self.assertEqual(bundle.pages[0].text.count("[REDACTED:CITIZENSHIP-NO]"), 1)

    def test_a_table_is_redacted_in_its_markdown_caption_and_header(self):
        bundle = _bundle(
            pages=[],
            tables=[
                TableRecord(
                    page_no=12,
                    # The header must name the identifier strictly — ``नागरिकता``
                    # followed by ``नं.``/``नम्बर``/``प्रमाणपत्र``. A bare
                    # ``नागरिकता`` deliberately does NOT match: a header governs
                    # every cell under it, so a loose label has a much larger
                    # over-redaction radius than a loose inline one.
                    markdown=(
                        "| नाम | नागरिकता नं. |\n| --- | --- |\n"
                        "| राम बहादुर | १२-०१-७५-०४५६७ |"
                    ),
                    header=["नाम", "नागरिकता नं."],
                    caption=CITIZENSHIP,
                )
            ],
        )

        redact_bundle(bundle)

        table = bundle.tables[0]
        # The value sits in a cell away from its label, which is the whole reason
        # the table pass exists — the inline pass cannot see it.
        self.assertNotIn("१२-०१-७५-०४५६७", table.markdown)
        self.assertIn("[REDACTED:TABLE-CITIZENSHIP-NO]", table.markdown)
        # The caption is prose and goes through the inline pass.
        self.assertNotIn("१२-०१-७५-०४५६७", table.caption)

    def test_a_header_cell_carrying_its_own_label_and_value_is_redacted(self):
        bundle = _bundle(
            pages=[],
            tables=[
                TableRecord(
                    page_no=12,
                    markdown="| x |",
                    header=["नागरिकता नं. १२-०१-७५-०४५६७"],
                )
            ],
        )

        redact_bundle(bundle)

        self.assertEqual(
            bundle.tables[0].header, ["नागरिकता नं. [REDACTED:CITIZENSHIP-NO]"]
        )

    def test_a_bare_value_with_no_label_anywhere_is_not_reachable(self):
        # Neither pass can touch this, and that is the redactor's precision
        # guard rather than a gap in the wiring: the inline pass needs a label in
        # the same span, and the table pass needs a labelled column to govern the
        # cell. A digit run on its own is also a case number, an amount, or a
        # clause reference. Recorded so nobody reads "redacted" as "scrubbed".
        bundle = _bundle(
            pages=[], tables=[TableRecord(page_no=1, header=["१२-०१-७५-०४५६७"])]
        )

        redact_bundle(bundle)

        self.assertEqual(bundle.tables[0].header, ["१२-०१-७५-०४५६७"])

    def test_the_page_char_count_follows_the_text_that_is_stored(self):
        bundle = _bundle()

        redact_bundle(bundle)

        # ``chars`` is served to clients beside the text; leaving the pre-redaction
        # count would describe a page that was never stored.
        self.assertEqual(bundle.pages[0].chars, len(bundle.pages[0].text))

    def test_an_empty_page_is_left_alone(self):
        bundle = _bundle(pages=[PageRecord(page_no=1, text="")])

        self.assertEqual(redact_bundle(bundle), {})
        self.assertEqual(bundle.pages[0].text, "")


class RedactionVersusFidelityTests(TestCase):
    """Redaction moves the number that decides whether a page looks readable.

    ``_fidelity`` keys on a page's Devanagari share and a placeholder is ASCII, so
    every removal pushes a page towards ``suspect`` — a removal reported as a
    failed decode. ``extract_pdf`` judges before it redacts for that reason, and
    likhit's own quality module strips placeholders before measuring for the same
    one (measured there: one synthetic document went ``clean`` to ``garbled``).

    **This is not hypothetical.** On a charge-sheet annex that is nothing but an
    identifier table, the share falls from 0.531 to 0.029 against a floor of
    0.15 — the page flips to ``suspect``. Measured the other way round, a
    perfectly-read annex would be published as a failed decode.
    """

    class _Analysis:
        has_suspicious_text_layer = True

    def test_judging_after_redaction_would_call_a_read_page_suspect(self):
        annex = "| क्र.सं. | नागरिकता नं. |\n| --- | --- |\n" + "\n".join(
            f"| {i} | १२-०१-७५-०४५६{i} |" for i in range(1, 15)
        )
        redacted, counts = redact_text(annex)

        # Keyed on the removal journal's kind, which is the same `citizenship`
        # whether the inline pass or the table pass took the value out — the
        # count is what matters here, and all fourteen are table cells.
        self.assertEqual(counts["citizenship"], 14)
        # 0.531 -> 0.029 against a floor of 0.15: fourteen Devanagari identifiers
        # become fourteen ASCII placeholders and the page stops looking Nepali.
        self.assertGreater(_devanagari_share(annex), 0.15)
        self.assertLess(_devanagari_share(redacted), 0.15)
        # So the page flips. THIS is why extract_pdf judges fidelity before it
        # redacts: measured the other way round, a perfectly-read annex is
        # published as a failed decode.
        self.assertEqual(_fidelity(annex, self._Analysis()), "repaired")
        self.assertEqual(_fidelity(redacted, self._Analysis()), "suspect")


class ResidualGateTests(TestCase):
    """Running the redactor is not the same as having removed everything.

    likhit's detector is wider than its removers, so a labelled value whose shape
    no rule anchors is found and left. Measured on the live charge-sheet shelf,
    67 of 883 documents came back that way — and until this gate existed, every
    one of them was stored and indexed as redacted with the data still in it.

    The check is the one likhit's own ``redact_tree`` makes and its single-call
    ``redact_text`` does not, which is why it lives at the boundary every writer
    crosses rather than in whichever caller remembered.
    """

    SURVIVOR = Counter({"bank_account": 1})

    def _doc(self, text):
        return {"@id": "https://jawafdehi.org/material/probe/gate", "text": text}

    def test_a_clean_document_passes_through_with_its_text(self):
        doc, original, _counts = redact_jsonld(self._doc("नागरिकता नं. १२-०१-७५-०४५६७"))

        self.assertIn("[REDACTED:CITIZENSHIP-NO]", doc["text"])
        self.assertTrue(original)

    def test_residue_withholds_the_text_rather_than_publishing_it(self):
        """The write path's half: unattended, so it must not stop the ingest.

        The document still lands — its metadata, its PDF link, its place in the
        archive. What does not land is an unsafe transcript in the field the
        search indexer reads.
        """
        with mock.patch(
            "likhit.privacy.scan_text", return_value=(self.SURVIVOR, Counter())
        ):
            doc, original, _counts = redact_jsonld(self._doc("कुनै पाठ"))

        self.assertNotIn("text", doc)
        self.assertEqual(doc["@id"], "https://jawafdehi.org/material/probe/gate")
        # Unpublished, not destroyed: the original still goes to source_text,
        # which is behind materials.view_unredacted.
        self.assertIn("कुनै पाठ", original)

    def test_residue_raises_for_a_caller_that_asked_to_be_told(self):
        """The sweep's half: a public row's transcript is a human's call."""
        with mock.patch(
            "likhit.privacy.scan_text", return_value=(self.SURVIVOR, Counter())
        ):
            with self.assertRaises(RedactionIncomplete) as caught:
                redact_jsonld(self._doc("कुनै पाठ"), on_residue="raise")

        self.assertEqual(caught.exception.residual["bank_account"], 1)
        # Counts, never values — this gets logged.
        self.assertNotIn("कुनै पाठ", str(caught.exception))

    def test_a_pan_does_not_withhold_anything(self):
        """The exemption has to hold at the gate, or PAN becomes un-publishable.

        No tier removes a PAN, so one left in the text is the system working. If
        the gate counted it, every procurement document carrying a contractor's
        registration would lose its transcript.
        """
        doc, _original, _counts = redact_jsonld(
            self._doc("स्थायी लेखा नम्बर (PAN): ६०१२३४५६७")
        )

        self.assertIn("text", doc)
        self.assertIn("६०१२३४५६७", doc["text"])
