from casework.common.order_windows import (
    ENTITY_WINDOW_CHARS, WINDOW_OVERLAP, Window, analysis_start, caption_end,
    end_windows, holding_start, start_windows,
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


def test_holding_falls_back_to_the_last_holding_verbs_sentence():
    text = "क" * 10_000 + "।कसूर गरेको ठहर्छ ।" + "ख" * 2_000
    pos, anchor = holding_start(text)
    assert anchor == "holding-verb" and pos == text.index("कसूर")


def test_the_holding_verb_window_keeps_an_acquittals_subject():
    text = "क" * 10_000 + "।\nप्रतिवादी रामले आरोपित कसुरबाट सफाई पाउने ठहर्छ ।" + "ख" * 2_000
    pos, _anchor = holding_start(text)
    assert text[pos:].startswith("प्रतिवादी रामले आरोपित कसुरबाट सफाई पाउने ठहर्छ")


def test_the_later_of_danda_and_newline_starts_the_sentence():
    text = "क" * 10_000 + "।पहिलो\nदोस्रो कसूर ठहर्छ" + "ख" * 100
    assert holding_start(text)[0] == text.index("दोस्रो")
    text = "क" * 10_000 + "\nपहिलो।दोस्रो कसूर ठहर्छ" + "ख" * 100
    assert holding_start(text)[0] == text.index("दोस्रो")


def test_the_sentence_start_never_backs_into_the_caption():
    text = "मुद्दा: भ्रष्टाचार" + "क" * 10_000 + "कसूर ठहर्छ" + "ख" * 100
    assert holding_start(text)[0] == caption_end(text)


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
