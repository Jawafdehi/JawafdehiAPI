"""Detect the Jawafdehi case type from its court forum and its sources.

Per VOL-3 operator guidance (latest), cases fall into FOUR families:

  1. CIAA_BASIC       — a CIAA (अख्तियार) case for which only the CIAA press
                        release is available (or, for a case still being built,
                        no documents yet). No charge sheet, no special-court
                        verdict.
  2. CIAA_EXTENDED    — a CIAA case where the charge sheet (अभियोग पत्र) is
                        available, but the full text of the special-court
                        verdict is NOT yet available.
  3. CIAA_HAS_VERDICT — a CIAA case carried all the way through: the full text
                        of the special-court verdict (फैसला) / order (आदेश) is
                        available. These carry the most detailed information, so
                        the bar is highest.
  4. NON_CIAA         — cases not tried in a CIAA forum (e.g. the Rabi
                        Lamichhane cooperative-fraud cases, or a constitutional
                        writ before the Supreme Court).

The **family** (CIAA vs NON_CIAA) is decided first and foremost by the *court
forum* the case is filed in — read from the ``court_cases`` IRIs — not by a
keyword in a source title. The Special Court (विशेष अदालत) tries CIAA
corruption cases, and a Supreme-Court corruption appeal (``…-CR-…``) is that
same case on appeal; both are CIAA forums. A Supreme-Court constitutional writ
(रिट — ``WC``/``WO``/``WF``/``WH``) or an ordinary district/high court is not.
This makes a source that merely *names* the CIAA (e.g. a news report that a
complaint was filed) unable, on its own, to promote a case into the CIAA
family — while still correctly typing the ~thousands of Special-Court cases
that have a court number but no attached documents yet.

The **tier** within the CIAA family (BASIC / EXTENDED / HAS_VERDICT) is then
driven by which documents are attached, read from each source's
``material_type`` (with a title-keyword fallback), so a mistyped or
oddly-titled charge sheet / verdict is still recognised.

It returns a dict the scorer and UI can consume.
"""

import re

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

# Supreme-Court constitutional-writ registers (रिट). A case tried only under one
# of these is a writ petition, not a CIAA corruption prosecution.
_SUPREME_WRIT_CODES = {"wc", "wo", "wf", "wh", "ws", "wm"}


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


def _forum_of(iri):
    """Classify a single court_case IRI into a forum bucket.

    IRIs look like ``https://jawafdehi.org/courtcase/<court>/<number>`` where
    ``<court>`` is e.g. ``special`` / ``supreme`` / ``kathmandudc`` / ``patanhc``
    and ``<number>`` embeds a register code (``081-cr-0123``, ``075-wo-0696``).
    """
    parts = [p for p in str(iri).lower().split("/") if p]
    court = num = ""
    if "courtcase" in parts:
        i = parts.index("courtcase")
        court = parts[i + 1] if i + 1 < len(parts) else ""
        num = parts[i + 2] if i + 2 < len(parts) else ""
    elif len(parts) >= 2:
        court, num = parts[-2], parts[-1]
    elif parts:
        court = parts[-1]

    if court == "special":
        return "special"
    if court == "supreme":
        m = re.search(r"[a-z]{1,3}", num)
        code = m.group(0) if m else ""
        if code in _SUPREME_WRIT_CODES:
            return "supreme_writ"
        if code == "cr":
            return "supreme_appeal"
        return "supreme_other"
    if court:
        return "ordinary"  # district / high court / any other forum
    return "unknown"


def _court_forum(court_cases):
    """Reduce all of a case's court refs to one forum label, CIAA forums first.

    Special Court presence always wins (it is the trying court); a Supreme-Court
    corruption appeal wins over a writ/ordinary ref on the same case.
    """
    forums = {_forum_of(ref) for ref in (court_cases or [])}
    for pref in (
        "special",
        "supreme_appeal",
        "supreme_writ",
        "supreme_other",
        "ordinary",
    ):
        if pref in forums:
            return pref
    return "none"


def detect(case):
    """Return {type, label, signals:{...}, rationale} for a case dict."""
    tt = _titles_and_types(case)
    court_cases = case.get("court_cases") or []
    forum = _court_forum(court_cases)

    has_press_release = any(
        st == "PRESS_RELEASE" or (_any(t, _PRESS_RELEASE) and _any(t, _CIAA))
        for t, st in tt
    )
    has_ciaa_source = any(_any(t, _CIAA) for t, _ in tt)
    # Prefer the typed material_type; fall back to a title-keyword match. A
    # material explicitly typed press_release is never counted as a charge
    # sheet, even if its title mentions the charge (अभियोग) — a press release
    # (प्रेस विज्ञप्ति) is the announcement, not the indictment.
    has_chargesheet = any(
        st == "CHARGE_SHEET" or (_any(t, _CHARGESHEET) and st != "PRESS_RELEASE")
        for t, st in tt
    )
    has_verdict = any(_any(t, _VERDICT) for t, _ in tt)
    has_court_order = any(st == "COURT_ORDER" or _any(t, _COURT_ORDER) for t, st in tt)
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
        "court_forum": forum,
    }

    # --- Family: decided by the court forum, not by a keyword in a source. ---
    if forum in ("special", "supreme_appeal"):
        is_ciaa = True
    elif forum in ("supreme_writ", "supreme_other", "ordinary"):
        # Tried in a non-CIAA forum: a stray CIAA mention in a source (e.g. a
        # news report that someone lodged a complaint) does not make it a CIAA
        # prosecution.
        is_ciaa = False
    else:
        # No court reference recorded — fall back to attached CIAA documents /
        # an explicit special-court reference in a source. A bare CIAA *mention*
        # is intentionally NOT enough on its own.
        is_ciaa = has_chargesheet or has_press_release or has_special_court

    if is_ciaa and has_verdict_text:
        # Strongest / most detailed family: the special-court verdict full text
        # is available.
        ctype, label = "CIAA_HAS_VERDICT", "CIAA case (special-court verdict)"
        rationale = (
            "CIAA case carried all the way through: the special-court verdict "
            "(फैसला) / order (आदेश) full text is attached. Highest-detail bar."
        )
    elif is_ciaa and has_chargesheet:
        # Charge sheet attached, but no full verdict text yet.
        ctype, label = "CIAA_EXTENDED", "CIAA case (extended — charge sheet)"
        rationale = (
            "CIAA case with the charge sheet (अभियोग पत्र) attached, but no full "
            "special-court verdict text yet."
        )
    elif is_ciaa:
        # A CIAA-forum case for which only the press release — or, for a case
        # still being built, nothing yet — is attached.
        ctype, label = "CIAA_BASIC", "CIAA case (basic)"
        rationale = (
            "CIAA (अख्तियार / विशेष अदालत) case for which no charge sheet and no "
            "special-court verdict record are attached yet."
        )
    else:
        ctype, label = "NON_CIAA", "Non-CIAA case"
        rationale = (
            "Not tried in a CIAA forum (Supreme-Court writ, ordinary court, or "
            "no CIAA charge sheet / press release); treated as a non-CIAA case."
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
