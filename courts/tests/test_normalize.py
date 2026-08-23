"""Unit tests for ``courts.normalize`` helpers."""

import pytest

from courts.normalize import (
    normalize_case_type,
    parse_stated_defendant_count,
    split_case_subject,
)


def test_parse_stated_count_ascii_and_devanagari():
    # "<lead> समेत N" states the court's true defendant total; Devanagari digits
    # are normalized to ASCII before parsing.
    assert parse_stated_defendant_count("प्रमुख प्रतिवादी समेत ३७") == (37, False)
    assert parse_stated_defendant_count("प्रमुख प्रतिवादी समेत 98") == (98, False)


def test_parse_stated_count_bare_samet_is_unknown():
    # A trailing bare "समेत" (no number) = truncated, magnitude unknown.
    assert parse_stated_defendant_count("प्रमुख प्रतिवादी समेत") == (None, True)


def test_parse_stated_count_bare_samet_with_trailing_punctuation():
    # Scraped cells may carry a trailing danda / full stop after "समेत".
    assert parse_stated_defendant_count("प्रमुख प्रतिवादी समेत।") == (None, True)
    assert parse_stated_defendant_count("प्रमुख प्रतिवादी समेत ॥") == (None, True)


def test_parse_stated_count_double_samet_takes_the_number():
    # Some cells repeat "समेत"; the stated number is still recovered.
    assert parse_stated_defendant_count("प्रमुख प्रतिवादी समेत ६४ समेत") == (64, False)


def test_parse_stated_count_no_signal():
    # A complete, non-truncated cell carries no signal.
    assert parse_stated_defendant_count("प्रमुख प्रतिवादी") == (None, False)
    assert parse_stated_defendant_count("") == (None, False)
    assert parse_stated_defendant_count(None) == (None, False)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # भ्रष्टाचार ( X ) wrapper -> inner offense.
        ("भ्रष्टाचार ( रकम हिनामिना )", "रकम हिनामिना"),
        # Leading/trailing structured case-number token stripped, label kept.
        ("080-cp-1852 लेनदेन", "लेनदेन"),
        ("074-CP-1181, भुल वा त्रुटि शंशोधन", "भुल वा त्रुटि शंशोधन"),
        ("(079-C1-0427) अपराधिक उपद्रव", "अपराधिक उपद्रव"),
        ("०७९-CP-२३४२ लेनदेन", "लेनदेन"),  # Devanagari-digit case number
        ("अपराधिक उपद्रव (079-C1-0427)", "अपराधिक उपद्रव"),
        # A separator (comma) before a trailing token is consumed whole — no
        # dangling "लेनदेन," left behind.
        ("लेनदेन, 080-cp-1852", "लेनदेन"),
        ("भुल सुधार; 074-CP-1181", "भुल सुधार"),
        # Combined noise (leading case number AND भ्रष्टाचार wrapper): stripping the
        # number re-exposes the wrapper, so both are resolved in a single call.
        ("080-cp-1852 भ्रष्टाचार ( रकम हिनामिना )", "रकम हिनामिना"),
        # A case number sits INSIDE a parenthetical alongside real text: the trailing
        # ", TOKEN)" is stripped AND the now-orphaned opening "(" is dropped, so no
        # unbalanced paren is left behind while the description is preserved.
        ("हाजिर गराई पाउ ( ज्यान मार्ने उद्योग, 079-C1-0213)", "हाजिर गराई पाउ ज्यान मार्ने उद्योग"),
        ("हाजिर गराई पाउ ( ज्यान मार्ने उद्योग, 079-C1-0229)", "हाजिर गराई पाउ ज्यान मार्ने उद्योग"),
        # A BALANCED inner paren (here from the भ्रष्टाचार wrapper unwrap) is preserved
        # verbatim — only a genuinely orphaned opening paren is removed.
        ("भ्रष्टाचार ( रिसवत(घुस) )", "रिसवत(घुस)"),
    ],
)
def test_normalize_case_type_strips_case_numbers(raw, expected):
    assert normalize_case_type(raw) == expected


@pytest.mark.parametrize(
    "value",
    [
        # Statute citations are the MOST useful case_type values — never touched,
        # even though they contain digits and parentheses.
        "चोरी गरेको (दफा 241)",
        "जबरजस्ती करणी गरेको (दफा 219)",
        "अंशबण्डा गरिपाऊँ (दफा २०५), (२१५)",
        "मु.फौ.सं. को दफा 155 बमोजिमको निवेदन",
        # Section references and clean labels pass through.
        "१५५ को सुविधा पाउँ",
        "155 बमोजिमको निवेदन",
        "चेक अनादर",
        # A value that is ONLY a case number has no label to keep -> unchanged
        # (never emptied to something misleading).
        "080-c1-0199",
        "3942",
        # A value carrying its OWN unbalanced paren but NO case-number token is left
        # verbatim: the orphan-paren cleanup only fires as a consequence of a strip,
        # so a value nothing else touched is never rebalanced.
        "जाँच बुझ ( अपुरो विवरण",
    ],
)
def test_normalize_case_type_preserves_meaningful_values(value):
    assert normalize_case_type(value) == value


def test_normalize_case_type_is_idempotent():
    for raw in (
        "भ्रष्टाचार ( रकम हिनामिना )",
        "080-cp-1852 लेनदेन",
        "चोरी गरेको (दफा 241)",
        "080-cp-1852 भ्रष्टाचार ( रकम हिनामिना )",  # combined noise
        "हाजिर गराई पाउ ( ज्यान मार्ने उद्योग, 079-C1-0213)",  # dangling-paren cleanup
    ):
        once = normalize_case_type(raw)
        assert normalize_case_type(once) == once


@pytest.mark.parametrize(
    "value",
    [
        "चेक  अनादर",  # internal double space
        " चेक अनादर ",  # leading/trailing whitespace
        '"लेनदेन"',  # surrounding quotes
        "  080-c1-0199  ",  # pure case number with padding
    ],
)
def test_normalize_case_type_cosmetic_only_returns_input_verbatim(value):
    # Whitespace / surrounding quotes are NOT structural noise: with no
    # case-number token or wrapper to strip, the raw input is returned unchanged,
    # so the importer does not rewrite / re-archive / re-count it for cosmetics.
    assert normalize_case_type(value) == value


@pytest.mark.parametrize("value", ["", None])
def test_normalize_case_type_empty(value):
    assert normalize_case_type(value) == value


# ── case_subject → (charge, statute_section) ─────────────────────────────────
#
# Every input below is a real live value (or the documented truncation of one)
# taken from the corpus measurement in the tag-crowding work, not invented shapes.
# The four ``ठगी गरेको …`` variants are the point of the exercise: on production
# they render as four separate filter chips (1,126 + 702 + 225 + 741) for one
# charge that should be a single bucket at ~2,794.

_THAGI = "ठगी गरेको"

# 1,126 — mixed-script digits: 24९ is Latin 2, Latin 4, Devanagari ९.
_VARIANT_NAME = "ठगी गरेको (आफ्नो नाम, दर्जा, पदवी, योग्यता ढाँटी) (दफा 24९(३)(ख))"
# 225 — NESTED parens inside the descriptive group, all-Latin statute digits.
_VARIANT_NESTED = (
    "ठगी गरेको (खण्ड (क) वा (ख) मा लेखिएदेखि बाहेक अन्य कुनै किसिमले ठगी गरेमा) "
    "(दफा 249 (3)(ग))"
)
# 702 — carries an internal TRIPLE space inside the group.
_VARIANT_TRIPLE_SPACE = (
    "ठगी गरेको (नेपाल सरकार वा नेपाल सरकारको पूर्ण वा   अधिकांश स्वामित्व भएको "
    "संस्थाको सम्पत्ति ठगी गरेको) (दफा 249(1)(क))"
)
# 741 — the bare charge, no parenthetical at all.
_VARIANT_BARE = "ठगी गरेको"


def test_split_case_subject_lifts_the_statute_out_of_the_last_group():
    charge, statute = split_case_subject(_VARIANT_NAME)
    assert charge == _THAGI
    # Mixed-script digits folded to one script; no space, no दफा marker.
    assert statute == "249(3)(ख)"


def test_split_case_subject_handles_nested_parentheses():
    """A regex stripping to the first ``)`` cuts this value in half and leaves
    ``वा (ख) मा … गरेमा)`` behind as prose, which is why this uses depth counting."""
    charge, statute = split_case_subject(_VARIANT_NESTED)
    assert charge == _THAGI
    assert statute == "249(3)(ग)"


def test_split_case_subject_survives_an_internal_triple_space():
    charge, statute = split_case_subject(_VARIANT_TRIPLE_SPACE)
    assert charge == _THAGI
    assert statute == "249(1)(क)"


def test_split_case_subject_with_no_statute_citation_at_all():
    assert split_case_subject(_VARIANT_BARE) == (_THAGI, None)
    # A trailing danda is hygiene, handled by the shared normalizer.
    assert split_case_subject("ठगी ।") == ("ठगी", None)
    assert split_case_subject("ठगी") == ("ठगी", None)


def test_the_four_thagi_variants_collapse_to_one_charge():
    """The headline outcome: four chips become one bucket."""
    charges = {
        split_case_subject(v)[0]
        for v in (_VARIANT_NAME, _VARIANT_NESTED, _VARIANT_TRIPLE_SPACE, _VARIANT_BARE)
    }
    assert charges == {_THAGI}
    # ...while their statutes stay distinct, which is the point of the new field.
    statutes = {
        split_case_subject(v)[1]
        for v in (_VARIANT_NAME, _VARIANT_NESTED, _VARIANT_TRIPLE_SPACE)
    }
    assert statutes == {"249(3)(ख)", "249(3)(ग)", "249(1)(क)"}


def test_the_same_citation_spelled_two_ways_lands_in_one_bucket():
    """``(दफा 24९(३)(ख))`` and ``(दफा 249 (3)(ख))`` are the same section."""
    a = split_case_subject("ठगी गरेको (दफा 24९(३)(ख))")[1]
    b = split_case_subject("ठगी गरेको (दफा 249 (3)(ख))")[1]
    assert a == b == "249(3)(ख)"


def test_split_case_subject_on_a_truncated_unterminated_group():
    """The corpus carries truncated subjects. An unclosed group must still yield
    its charge head rather than swallowing the value."""
    charge, statute = split_case_subject("ठगी गरेको (नेपाल सरकार वा नेपाल सरकारको पूर्ण वा")
    assert charge == _THAGI
    assert statute is None


def test_split_case_subject_with_no_charge_head_returns_none_not_a_guess():
    charge, statute = split_case_subject("(दफा 249(3)(ख))")
    assert charge is None
    assert statute == "249(3)(ख)"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_split_case_subject_empty(value):
    assert split_case_subject(value) == (None, None)


def test_split_case_subject_is_idempotent_on_its_own_charge():
    """The charge is re-normalized on every reindex, so it has to be a fixed point."""
    for value in (_VARIANT_NAME, _VARIANT_NESTED, _VARIANT_TRIPLE_SPACE, _VARIANT_BARE):
        charge = split_case_subject(value)[0]
        assert split_case_subject(charge)[0] == charge


def test_split_case_subject_keeps_a_non_statute_parenthetical_out_of_the_statute():
    """Only a group carrying ``दफा`` is a citation; a descriptive tail is not."""
    charge, statute = split_case_subject("कर छली गरेको (ठूला करदाता कार्यालय)")
    assert charge == "कर छली गरेको"
    assert statute is None
