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
from unittest import TestCase

from django.test import override_settings

from materials.redaction import RedactionUnavailable, redact_text

CITIZENSHIP = "नागरिकता नं. १२-०१-७५-०४५६७ रहेको"
DATE_OF_BIRTH = "जन्म मिति २०४५।०३।१२ रहेको"
MOBILE = "सम्पर्क ९८४१२३४५६७"
EMAIL = "इमेल: ram.bahadur@example.com.np"
BANK = "बैंक खाता नं. ०१२३४५६७८९०१२३"
PAN = "स्थायी लेखा नम्बर (PAN): ६०१२३४५६७"


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
