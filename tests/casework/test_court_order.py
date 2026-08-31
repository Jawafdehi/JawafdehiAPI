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
    THAHAR_CHARS,
    THAHAR_MARKER,
    court_order_head,
    court_order_tail,
    court_order_thahar,
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


class TestThaharWindow:
    def test_it_starts_at_the_marker(self):
        head = "क" * 50_000
        body = "ठहर खण्ड" + "ठ" * 500
        got = court_order_thahar(head + body, limit=16_000, label=False)
        assert got.startswith("ठहर खण्ड")
        assert "क" * 50_000 not in got

    def test_it_takes_limit_chars_forward_from_the_marker(self):
        text = "क" * 1_000 + "ठहर खण्ड" + "ठ" * 40_000
        got = court_order_thahar(text, limit=THAHAR_CHARS, label=False)
        assert len(got) == THAHAR_CHARS

    def test_a_document_with_no_marker_falls_back_to_the_tail(self):
        # 078-CR-0042 is a real example: no marker anywhere. The old code fell
        # back to head+tail there too, and that case measured clean.
        text = "क" * 40_000 + "अन्तिम"
        got = court_order_thahar(text, limit=THAHAR_CHARS, label=False)
        assert got.endswith("अन्तिम")
        assert len(got) == THAHAR_CHARS

    def test_a_short_document_comes_back_whole(self):
        text = "क" * 100 + "ठहर खण्ड" + "ठ" * 100
        assert court_order_thahar(text, limit=16_000, label=False) == text

    def test_empty_text_is_empty(self):
        assert court_order_thahar("", limit=16_000) == ""

    def test_the_marker_is_the_donor_literal(self):
        # Devanagari is data. This pins the exact string the old
        # `_truncate_court_order` anchored on -- a normalised or re-typed
        # variant silently stops matching and the window slides to the tail.
        assert THAHAR_MARKER == "ठहर खण्ड"


class TestSummariseVerdictLivesHere:
    """`summarize_verdict` used to live in `enrich_timeline` and
    `enrich_description` reached across to borrow it. `enrich_related_entities`
    never did, which is the whole reason its court-order handling was broken:
    a shared function that has to be reached for is a function that gets
    missed. Its home is here, beside the zone reader.
    """

    def test_importable_from_the_shared_home(self):
        from casework.common.court_order import summarize_verdict
        assert callable(summarize_verdict)

    def test_timeline_still_exposes_it_for_existing_importers(self):
        from casework.common.court_order import summarize_verdict as shared
        from casework.enrich_timeline import summarize_verdict as viatimeline
        assert viatimeline is shared

    def test_description_uses_the_shared_home(self):
        import casework.enrich_description as ed
        from casework.common.court_order import summarize_verdict as shared
        assert ed.summarize_verdict is shared

    def test_long_text_is_summarised_in_multiple_passes(self):
        # A single head-truncated pass drops the फैसला/ठहर, which sits at the
        # end. The chunked pass is the reason this function exists.
        from casework.common import court_order as co
        calls = []

        def fake_invoke(system, content, tier, usage, max_tokens):
            calls.append(content)
            return f"सारांश {len(calls)}"

        text = "क" * (co.VERDICT_SUMMARY_CHUNK_CHARS * 2 + 10)
        got = co.summarize_verdict(text, fake_invoke, usage=None)
        assert len(calls) == 3
        assert "खण्ड 1/3" in got and "खण्ड 3/3" in got

    def test_a_failed_chunk_does_not_renumber_the_survivors(self):
        from casework.common import court_order as co
        seen = []

        def fake_invoke(system, content, tier, usage, max_tokens):
            seen.append(content)
            if len(seen) == 2:
                raise RuntimeError("provider 502")
            return f"सारांश {len(seen)}"

        text = "क" * (co.VERDICT_SUMMARY_CHUNK_CHARS * 2 + 10)
        got = co.summarize_verdict(text, fake_invoke, usage=None)
        assert "खण्ड 1/3" in got
        assert "खण्ड 3/3" in got
        assert "खण्ड 2/3" not in got

    def test_total_failure_returns_none(self):
        from casework.common import court_order as co

        def always_fails(system, content, tier, usage, max_tokens):
            raise RuntimeError("provider down")

        assert co.summarize_verdict("क" * 20_000, always_fails, usage=None) is None
