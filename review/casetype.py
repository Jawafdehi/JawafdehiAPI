"""Detect the Jawafdehi case type from its sources and court references.

Per VOL-3 operator guidance (latest), cases fall into FOUR families:

  1. CIAA_BASIC       — a CIAA (अख्तियार) case for which only the CIAA press
                        release is available. No charge sheet, no special-court
                        record.
  2. CIAA_EXTENDED    — a CIAA case where the CIAA press release AND the charge
                        sheet (अभियोग पत्र) are available, but the full text of
                        the special-court verdict is NOT yet available.
  3. CIAA_HAS_VERDICT — a CIAA case carried all the way through: the full text
                        of the special-court verdict (फैसला) / order (आदेश) is
                        available. These carry the most detailed information, so
                        the bar is highest.
  4. NON_CIAA         — cases not initiated by the CIAA (e.g. the Rabi
                        Lamichhane cooperative-fraud cases). No CIAA press
                        release and no CIAA charge sheet.

Detection is heuristic, driven by source titles/types and the court_cases
field. It returns a dict the scorer and UI can consume.
"""

# Nepali + English signal keywords (lower-cased substring match).
_PRESS_RELEASE = [
    "प्रेश विज्ञप्त",
    "प्रेस विज्ञप्त",
    "press release",
    "press statement",
]
_CIAA = ["अख्तियार", "ciaa", "अख्तियारको", "commission for the investigation"]
_CHARGESHEET = ["अभियोग", "charge sheet", "chargesheet", "अभियोगपत्र"]
_VERDICT = ["फैसला", "verdict", "judgement", "judgment"]
_COURT_ORDER = ["आदेश", "court order", "अदालतको आदेश"]
_SPECIAL_COURT = ["विशेष अदालत", "special court"]


def _titles_and_types(case):
    out = []
    for ev in case.get("evidence", []) or []:
        # Evidence entries carry a resolved `material` dict (ADR: cases own no
        # documents): display_name + material_type replace title + source_type.
        mat = ev.get("material") or {}
        out.append(
            (
                (mat.get("display_name") or "").lower(),
                (mat.get("material_type") or "").upper(),
            )
        )
    return out


def _any(text, keywords):
    return any(k.lower() in text for k in keywords)


def detect(case):
    """Return {type, label, signals:{...}, rationale} for a case dict."""
    tt = _titles_and_types(case)
    court_cases = case.get("court_cases") or []

    has_press_release = any(
        (_any(t, _PRESS_RELEASE) and (_any(t, _CIAA) or st == "OFFICIAL_GOVERNMENT"))
        or _any(t, _CIAA)
        and _any(t, _PRESS_RELEASE)
        for t, st in tt
    )
    has_ciaa_source = any(_any(t, _CIAA) for t, _ in tt)
    has_chargesheet = any(_any(t, _CHARGESHEET) for t, _ in tt)
    has_verdict = any(_any(t, _VERDICT) for t, _ in tt)
    has_court_order = any(_any(t, _COURT_ORDER) for t, _ in tt)
    has_special_court = any(_any(t, _SPECIAL_COURT) for t, _ in tt)
    has_court_case_no = bool(court_cases)

    # The full text of a special-court verdict / order being attached is what
    # distinguishes a HAS_VERDICT case from a charge-sheet-only EXTENDED case.
    has_verdict_text = has_verdict or has_court_order

    signals = {
        "ciaa_press_release": has_press_release,
        "ciaa_source": has_ciaa_source,
        "charge_sheet": has_chargesheet,
        "court_verdict": has_verdict,
        "court_order": has_court_order,
        "special_court_ref": has_special_court,
        "court_case_number": has_court_case_no,
        "verdict_text_available": has_verdict_text,
    }

    is_ciaa = has_chargesheet or has_press_release or has_ciaa_source

    if is_ciaa and has_verdict_text:
        # Strongest / most detailed family: the special-court verdict full text
        # is available.
        ctype, label = "CIAA_HAS_VERDICT", "CIAA case (special-court verdict)"
        rationale = (
            "CIAA case carried all the way through: the special-court verdict "
            "(फैसला) / order (आदेश) full text is attached. Highest-detail bar."
        )
    elif is_ciaa and has_chargesheet:
        # Press release + charge sheet, but no full verdict text yet.
        ctype, label = "CIAA_EXTENDED", "CIAA case (extended — charge sheet)"
        rationale = (
            "CIAA case with both the press release and the charge sheet "
            "(अभियोग पत्र) attached, but no full special-court verdict text yet."
        )
    elif is_ciaa:
        # Only the CIAA press release / CIAA source is available.
        ctype, label = "CIAA_BASIC", "CIAA case (basic)"
        rationale = (
            "CIAA (अख्तियार) case for which only the press release is available; "
            "no charge sheet and no special-court verdict record."
        )
    else:
        ctype, label = "NON_CIAA", "Non-CIAA case"
        rationale = (
            "No CIAA press release or charge sheet detected; treated as a "
            "non-CIAA case (e.g. police/other-agency or pre-charge)."
        )

    # A formal court case number is expected once a case reaches the court
    # (charge sheet filed): required for the EXTENDED and HAS_VERDICT families.
    court_number_required = ctype in ("CIAA_EXTENDED", "CIAA_HAS_VERDICT")

    return {
        "type": ctype,
        "label": label,
        "signals": signals,
        "rationale": rationale,
        "court_number_required": court_number_required,
    }
