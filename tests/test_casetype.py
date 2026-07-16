"""Unit tests for the review case-type classifier (`review.casetype.detect`).

The classifier decides the **family** (CIAA vs NON_CIAA) from the court forum
read off the ``court_cases`` IRIs, then the **tier** (BASIC / EXTENDED /
HAS_VERDICT) from the attached documents' ``material_type``. A source that
merely names the CIAA must not, on its own, promote a case into the CIAA family.
"""

from review.casetype import detect


def _iri(court, number):
    return f"https://jawafdehi.org/courtcase/{court}/{number}"


def _case(court_cases=None, evidence=None):
    return {"court_cases": court_cases or [], "evidence": evidence or []}


def _src(display_name, material_type):
    return {"material": {"display_name": display_name, "material_type": material_type}}


# --- Family from court forum -------------------------------------------------


def test_special_court_number_is_ciaa_even_without_sources():
    # A Special-Court case number with no attached documents (the ~2.6k draft
    # backlog) must still be recognised as CIAA — at the BASIC tier.
    res = detect(_case(court_cases=[_iri("special", "081-cr-0123")]))
    assert res["type"] == "CIAA_BASIC"
    assert res["signals"]["court_forum"] == "special"


def test_old_format_special_number_is_ciaa():
    # Legacy special-court numbering (93-068-0194) has no CR code but is still
    # the Special Court.
    res = detect(_case(court_cases=[_iri("special", "93-068-0194")]))
    assert res["type"].startswith("CIAA")
    assert res["signals"]["court_forum"] == "special"


def test_supreme_writ_is_non_ciaa_despite_ciaa_mention_in_source():
    # Giribandhu shape: a Supreme-Court writ whose only CIAA marker is a news
    # source mentioning the CIAA, plus attached SC verdict orders. Must be
    # NON_CIAA — the forum wins over the keyword.
    res = detect(
        _case(
            court_cases=[_iri("supreme", "078-wc-0004"), _iri("supreme", "075-wo-0696")],
            evidence=[
                _src("Student org files complaint in CIAA against X", "document"),
                _src("Supreme Court verdict on case no. 078-WC-0004", "court_order"),
            ],
        )
    )
    assert res["type"] == "NON_CIAA"
    assert res["signals"]["court_forum"] == "supreme_writ"
    # the signal is still reported, just not used to classify the family
    assert res["signals"]["ciaa_source"] is True


def test_supreme_corruption_appeal_is_ciaa():
    res = detect(_case(court_cases=[_iri("supreme", "080-cr-0081")]))
    assert res["type"].startswith("CIAA")
    assert res["signals"]["court_forum"] == "supreme_appeal"


def test_ordinary_courts_are_non_ciaa():
    # Rabi Lamichhane shape: district + high + supreme writ/review, no special.
    res = detect(
        _case(
            court_cases=[
                _iri("kathmandudc", "080-c4-2408"),
                _iri("patanhc", "081-re-1730"),
                _iri("supreme", "081-wh-0320"),
            ]
        )
    )
    assert res["type"] == "NON_CIAA"


def test_special_wins_when_mixed_with_writ():
    res = detect(
        _case(court_cases=[_iri("supreme", "078-wc-0004"), _iri("special", "081-cr-0123")])
    )
    assert res["type"].startswith("CIAA")
    assert res["signals"]["court_forum"] == "special"


# --- Tier from attached documents -------------------------------------------


def test_typed_charge_sheet_gives_extended_even_if_title_lacks_keyword():
    # 081-CR-0123 shape: charge_sheet-typed sources whose titles do not contain
    # the अभियोग keyword (one says आरोप, one uses a malformed spelling).
    res = detect(
        _case(
            court_cases=[_iri("special", "081-cr-0123")],
            evidence=[
                _src("... अख्तियारको आरोप ...", "charge_sheet"),
                _src("अभियाेग पत्र", "charge_sheet"),  # decomposed vowel signs
            ],
        )
    )
    assert res["type"] == "CIAA_EXTENDED"
    assert res["signals"]["charge_sheet"] is True


def test_court_order_type_gives_has_verdict():
    res = detect(
        _case(
            court_cases=[_iri("special", "081-cr-0200")],
            evidence=[_src("फैसलाको प्रति", "court_order")],
        )
    )
    assert res["type"] == "CIAA_HAS_VERDICT"


# --- No court reference: source fallback ------------------------------------


def test_no_court_ref_press_release_is_ciaa_basic():
    res = detect(
        _case(evidence=[_src("CIAA press release on X", "press_release")])
    )
    assert res["type"] == "CIAA_BASIC"
    assert res["signals"]["court_forum"] == "none"


def test_no_court_ref_bare_ciaa_mention_is_not_ciaa():
    # A news article that merely names the CIAA, with no court number and no
    # charge sheet / press release, is NOT enough to classify as CIAA.
    res = detect(
        _case(evidence=[_src("News: CIAA files case against 10 individuals", "document")])
    )
    assert res["type"] == "NON_CIAA"
    assert res["signals"]["ciaa_source"] is True


def test_empty_case_is_non_ciaa():
    res = detect(_case())
    assert res["type"] == "NON_CIAA"
    assert res["signals"]["court_forum"] == "none"
