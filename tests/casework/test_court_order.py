"""The court-order zone reader.

WHY ZONES AND NOT ONE SLICE. Measured over 37 production court orders
(2026-08-31): the operative verdict sits a median 2,852 chars from the END of
the file, p90 8,389, max 38,792. The slice this replaces anchored on the
literal `ठहर खण्ड` and took 12,000 chars FORWARD, which reached that verdict in
only 10 of 37 orders. Median order length is 60,842 chars; the longest is
379,484.
"""
from casework.common.court_order import (
    HEAD_CHARS,
    TAIL_CHARS,
    court_order_head,
    court_order_tail,
)


class TestZoneSizes:
    def test_tail_is_25k(self):
        # 25,000 contains the last operative verb in 36 of 37 measured orders;
        # 10,000 reaches 34. Do not shrink this without re-measuring.
        assert TAIL_CHARS == 25_000

    def test_head_is_6k(self):
        # Caption, party list and bench sit in the first 3% of every order in
        # the sample (38/38). 6,000 clears that on a median 60,842-char order.
        assert HEAD_CHARS == 6_000


class TestCourtOrderTail:
    def test_none_passthrough(self):
        assert court_order_tail(None) == ""

    def test_empty_passthrough(self):
        assert court_order_tail("") == ""

    def test_short_text_returned_whole_and_unlabelled(self):
        text = "छोटो आदेश। ठहर्छ।"
        assert court_order_tail(text) == text

    def test_long_text_keeps_the_end_not_the_start(self):
        text = "क" * 30_000 + "यो ठहर खण्ड हो। ठहर्छ।"
        got = court_order_tail(text)
        assert got.endswith("यो ठहर खण्ड हो। ठहर्छ।")
        assert "क" * 30_000 not in got

    def test_long_text_is_capped_at_the_limit_in_chars_not_a_percentage(self):
        # THE PERCENTAGE TRAP. The tail is ~12% of a document. On the 379,484-
        # char order in the sample that is 45,538 chars -- far over
        # PROMPT_HARD_MAX. The cap must be absolute.
        text = "क" * 379_484
        got = court_order_tail(text, label=False)
        assert len(got) == TAIL_CHARS

    def test_label_marks_the_text_as_a_fragment(self):
        text = "क" * 30_000
        assert "[" in court_order_tail(text)

    def test_label_can_be_switched_off(self):
        text = "क" * 30_000
        assert court_order_tail(text, label=False).startswith("क")

    def test_respects_an_explicit_smaller_limit(self):
        text = "क" * 30_000
        assert len(court_order_tail(text, limit=1_000, label=False)) == 1_000


class TestCourtOrderHead:
    def test_none_passthrough(self):
        assert court_order_head(None) == ""

    def test_short_text_returned_whole(self):
        text = "वादी: नेपाल सरकार। प्रतिवादी: क ख।"
        assert court_order_head(text) == text

    def test_long_text_keeps_the_start_not_the_end(self):
        text = "वादी: नेपाल सरकार।" + "ख" * 30_000
        got = court_order_head(text, label=False)
        assert got.startswith("वादी: नेपाल सरकार।")
        assert len(got) == HEAD_CHARS

    def test_head_and_tail_of_the_same_order_do_not_overlap(self):
        text = "क" * 100_000
        head = court_order_head(text, label=False)
        tail = court_order_tail(text, label=False)
        assert len(head) + len(tail) < len(text)
