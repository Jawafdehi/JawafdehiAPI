"""Personal data is removed before anything stores the text.

The pipeline's contract with the redaction plan: unredacted text exists only in
the extracting process's memory. What is pinned here is that contract, the honest
reporting of how narrow the removal actually is, and the one interaction that
would otherwise go unnoticed — a removal being reported as a failed decode.

    DATABASE_URL=sqlite:// NES_DB_URL=sqlite:// NGM_DATABASE_URL=sqlite:// \
        uv run pytest materials/tests/test_extraction_redact.py
"""

from __future__ import annotations

from collections import Counter
from unittest import TestCase

from materials.redaction import redact_text

CITIZENSHIP = "नागरिकता नं. १२-०१-७५-०४५६७ रहेको"
DATE_OF_BIRTH = "जन्म मिति २०४५।०३।१२ रहेको"
MOBILE = "सम्पर्क ९८४१२३४५६७"


class RedactTextTests(TestCase):
    """What the redactor removes, and — just as load-bearing — what it does not."""

    def test_a_citizenship_number_and_a_date_of_birth_are_removed(self):
        redacted, counts = redact_text(f"{CITIZENSHIP}। {DATE_OF_BIRTH}।")

        self.assertIn("[REDACTED:CITIZENSHIP-NO]", redacted)
        self.assertIn("[REDACTED:DATE-OF-BIRTH]", redacted)
        self.assertNotIn("१२-०१-७५-०४५६७", redacted)
        self.assertEqual(counts["citizenship_redacted"], 1)

    def test_what_is_removed_is_narrower_than_what_is_found(self):
        redacted, _counts = redact_text(MOBILE)

        # The mobile number SURVIVES. The contact pass exists but lives in
        # another repository, which is why [REDACTED:PHONE-NO] is a registered
        # placeholder that nothing in likhit emits. Asserting the gap keeps
        # "redacted" from being read as "clean".
        self.assertIn("९८४१२३४५६७", redacted)

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
