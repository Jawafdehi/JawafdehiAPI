from pathlib import Path

from casework.common.order_windows import (
    ENTITY_WINDOW_CHARS, HOLDING_VERB_LEAD, WINDOW_OVERLAP, Window, analysis_start,
    caption_end, end_windows, holding_start, start_windows,
)


def test_caption_ends_after_the_case_type_line():
    text = "क" * 400 + "\nमुद्दा:- भ्रष्टाचार\n" + "ख" * 1000
    assert caption_end(text) == text.index("मुद्दा:") + len("मुद्दा:")


def test_caption_accepts_the_dha_spelling():
    text = "क" * 300 + "मुद्धा:- घुस" + "ख" * 100
    assert caption_end(text) == text.index("मुद्धा:") + len("मुद्धा:")


def test_no_caption_marker_falls_back_to_2000():
    assert caption_end("क" * 50_000) == 2_000


def test_a_short_order_is_one_window_whole():
    text = "मुद्दा: x\n" + "क" * 5_000
    windows = list(start_windows(text))
    assert len(windows) == 1 and windows[0].text == text


def test_first_window_is_caption_plus_30k_and_windows_overlap():
    text = "क" * 500 + "मुद्दा:" + "ख" * 100_000
    windows = list(start_windows(text))
    cap = caption_end(text)
    assert windows[0].start == 0 and windows[0].end == cap + ENTITY_WINDOW_CHARS
    for prev, nxt in zip(windows, windows[1:]):
        assert nxt.start == prev.end - WINDOW_OVERLAP
    assert windows[-1].end == len(text)


def test_windows_stop_at_the_limit_without_covering_a_huge_order():
    text = "मुद्दा:" + "ख" * 500_000
    windows = list(start_windows(text))
    assert len(windows) == 4 and windows[-1].end < len(text)


def test_a_window_ends_on_a_paragraph_break_when_one_is_near():
    body = "ख" * 29_000 + "\n" + "ग" * 40_000
    text = "मुद्दा:" + body
    first = next(start_windows(text))
    assert first.text.endswith("\n")


def test_holding_starts_at_the_last_tasartha():
    text = "तसर्थ पहिलो " + "क" * 10_000 + "तसर्थ अन्तिम" + "ख" * 3_000
    pos, anchor = holding_start(text)
    assert anchor == "तसर्थ" and pos == text.rindex("तसर्थ")


def test_holding_falls_back_to_a_fixed_lead_before_the_last_holding_verb():
    text = "क" * 10_000 + "।कसूर गरेको ठहर्छ ।" + "ख" * 2_000
    pos, anchor = holding_start(text)
    assert anchor == "holding-verb" and pos == text.index("ठहर्छ") - HOLDING_VERB_LEAD


def test_the_holding_verb_window_keeps_an_acquittals_subject():
    text = "क" * 10_000 + "।\nप्रतिवादी रामले आरोपित कसुरबाट सफाई पाउने ठहर्छ ।" + "ख" * 2_000
    pos, _anchor = holding_start(text)
    assert pos <= text.index("प्रतिवादी रामले")


def test_the_lead_snaps_forward_to_its_first_blank_line():
    text = ("क" * 10_000 + "\n\nपहिलो अनुच्छेद" + "ख" * 500 + "\n\nदोस्रो अनुच्छेद"
            + "ग" * 500 + "कसूर ठहर्छ" + "घ" * 100)
    assert holding_start(text)[0] == text.index("पहिलो")


def test_a_blank_line_before_the_lead_is_ignored():
    text = "क" * 10_000 + "\n\n" + "ख" * 4_000 + "कसूर ठहर्छ" + "ग" * 100
    assert holding_start(text)[0] == text.index("ठहर्छ") - HOLDING_VERB_LEAD


def test_a_currency_danda_does_not_start_the_holding():
    text = "क" * 10_000 + "सफाइ पाउने, रु.१०,०००।- जफत हुने ठहर्छ" + "ख" * 100
    assert holding_start(text)[0] == text.index("ठहर्छ") - HOLDING_VERB_LEAD
    text = "क" * 10_000 + "सफाइ पाउने, रु.१०,०००।– जफत हुने ठहर्छ" + "ख" * 100
    assert holding_start(text)[0] == text.index("ठहर्छ") - HOLDING_VERB_LEAD


def test_the_lead_never_backs_into_the_caption():
    text = "मुद्दा: भ्रष्टाचार" + "क" * 1_000 + "कसूर ठहर्छ" + "ख" * 100
    assert holding_start(text)[0] == caption_end(text)


ORDER_END_075_CR_0186 = (Path(__file__).parent / "fixtures" / "075-CR-0186-order-end.txt").read_text(
    encoding="utf-8")


def test_075_cr_0186_acquittal_is_inside_the_first_end_window():
    text = "मुद्दा: सम्पत्ति शुद्धीकरण\n" + ("क" * 79 + "\n") * 700 + ORDER_END_075_CR_0186
    first = end_windows(text)[0]
    assert "तीनै जना  प्रतिवादीहरुले  अभियोगदावीबाट  सफाइ\n   पाउने ठहर्छ" in first.text
    assert "जफत हुने ठहर्छ" in first.text


def test_holding_falls_back_to_the_tail():
    text = "क" * 50_000
    assert holding_start(text) == (50_000 - 18_000, "tail")


def test_end_windows_cover_the_holding_to_the_end_first():
    text = "क" * 50_000 + "तसर्थ" + "ख" * 20_000
    hold, _ = holding_start(text)
    windows = end_windows(text)
    assert windows[0].end == len(text)
    covered_from = min(w.start for w in windows if w.end > hold)
    assert covered_from <= hold
    starts = [w.start for w in windows]
    assert starts == sorted(starts, reverse=True)


def test_end_windows_never_read_back_past_the_analysis_start():
    text = ("क" * 3_000 + "मिसिल अध्ययन गरियो" + "ख" * 100_000
            + "तसर्थ" + "ग" * 5_000)
    floor = analysis_start(text)
    assert floor is not None
    assert all(w.start >= floor for w in end_windows(text))


def test_end_windows_read_back_at_most_max_back_chunks():
    text = "क" * 200_000 + "तसर्थ" + "ख" * 1_000
    hold, _ = holding_start(text)
    back = [w for w in end_windows(text, max_back=2) if w.start < hold]
    assert len(back) == 2


def test_label_names_the_character_range():
    w = Window(1_840, 31_840, 214_300, "x")
    assert w.label() == "अदालतको आदेश, अक्षर 1,840–31,840 (कुल 214,300)"
