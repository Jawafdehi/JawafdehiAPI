"""Deterministic rule detectors + applicability logic for the rule engine.

Each detector takes (case) and returns (score:int 0-100, issues:list[str]).
Detectors are referenced by name from a Rule.detector field. Deterministic
rules are exact, so their confidence is reported as variance 0 (std 0).

Applicability: a rule is active for a case iff its `applies_to` contains "ALL"
or the case's detected type.
"""

import re

GOLD_DESC_CHARS = 12000
GOLD_TIMELINE = 20
GOLD_SOURCES = 7
SUBSTANTIAL_DESC_CHARS = 600

COURT_RE = re.compile(r"\d{2,3}-[A-Za-z]{1,3}-\d{3,4}")

# Nepali + English signal keywords (lower-cased substring match).
_PRESS_RELEASE = [
    "प्रेश विज्ञप्त",
    "प्रेस विज्ञप्त",
    "press release",
    "press statement",
]
_CIAA = ["अख्तियार", "ciaa", "commission for the investigation"]
_CHARGESHEET = ["अभियोग", "charge sheet", "chargesheet"]
_VERDICT = ["फैसला", "verdict", "judgement", "judgment"]
_COURT_ORDER = ["आदेश", "court order"]


def _clamp(n):
    return max(0, min(100, int(round(n))))


def _source_titles(case):
    out = []
    for ev in case.get("evidence", []) or []:
        # Resolved material dict (display_name + material_type) replaces the old
        # nested source dict (title + source_type). ADR: cases own no documents.
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


def _has_court_number(case):
    court_cases = case.get("court_cases") or []
    return any(COURT_RE.search(str(c)) for c in court_cases), court_cases


# ----------------------- detectors -----------------------


def court_case_number(case):
    has_no, court_cases = _has_court_number(case)
    issues = []
    if not court_cases:
        issues.append(
            "No court case number in the court_cases field (required for this case type)."
        )
        return 0, issues
    if not has_no:
        issues.append(
            "court_cases present but not a well-formed NNN-XX-NNNN court case number."
        )
        return 45, issues
    return 100, issues


def court_number_in_title(case):
    """The case title must contain the special-court case number (e.g. 081-CR-0095).

    For CIAA cases the formal court case number must appear in the title itself,
    typically in parentheses, e.g.
    "टेरामक्स (TERAMOCS) खरिदमा भ्रष्टाचार मुद्दा (081-CR-0095)".
    When the case also lists court_cases, the number in the title should match
    one of them.
    """
    title = case.get("title") or ""
    title_matches = {m.group(0).upper() for m in COURT_RE.finditer(title)}
    if not title_matches:
        return 0, [
            "Title has no court case number (e.g. 081-CR-0095); CIAA case titles "
            "must include the special-court case number."
        ]
    # The number in the title MUST match the recorded court_cases number.
    court_numbers = set()
    for ref in case.get("court_cases") or []:
        for m in COURT_RE.finditer(str(ref)):
            court_numbers.add(m.group(0).upper())
    if not court_numbers:
        return 0, [
            "No court case number recorded in court_cases to match the title "
            f"number ({', '.join(sorted(title_matches))}) against."
        ]
    if not (title_matches & court_numbers):
        return 0, [
            "Title court number "
            f"({', '.join(sorted(title_matches))}) does not match the recorded "
            f"court_cases number(s) ({', '.join(sorted(court_numbers))})."
        ]
    return 100, []


def bigo_amount_present(case):
    """The bigo (बिगो) amount — total disputed/embezzled sum in NPR — must be set.

    Required for CIAA cases: the figure anchors the allegation. A missing or
    non-positive bigo fails the gate.
    """
    bigo = case.get("bigo")
    if bigo is None or bigo == "":
        return 0, ["Bigo (बिगो) amount is not set; it is required for CIAA cases."]
    try:
        value = int(bigo)
    except (TypeError, ValueError):
        return 0, [f"Bigo amount is not a valid number: {bigo!r}."]
    if value <= 0:
        return 0, [f"Bigo amount must be a positive figure (got {value})."]
    return 100, []


def additional_description(case):
    desc = (case.get("description") or "").strip()
    if not desc:
        return 0, [
            "Description is empty; a substantial additional description is required."
        ]
    pts = 30  # has something
    pts += min(len(desc) / SUBSTANTIAL_DESC_CHARS, 1.0) * 45
    pts += min(len(desc) / GOLD_DESC_CHARS, 1.0) * 25
    issues = []
    if len(desc) < SUBSTANTIAL_DESC_CHARS:
        issues.append(
            f"Description is thin ({len(desc)} chars); a substantial additional "
            f"description (≥{SUBSTANTIAL_DESC_CHARS}) is expected."
        )
    return _clamp(pts), issues


def structural_completeness(case):
    desc = (case.get("description") or "").strip()
    allegations = case.get("key_allegations") or []
    timeline = case.get("timeline") or []
    evidence = case.get("evidence") or []
    entities = case.get("entities") or []
    tags = case.get("tags") or []
    pts = 0
    pts += 14 if desc else 0
    pts += min(len(allegations) / 4.0, 1.0) * 24
    pts += min(len(timeline) / GOLD_TIMELINE, 1.0) * 22
    pts += min(len(evidence) / GOLD_SOURCES, 1.0) * 20
    pts += min(len(entities) / 3.0, 1.0) * 12
    pts += min(len(tags) / 3.0, 1.0) * 8
    issues = []
    if len(allegations) < 3:
        issues.append(f"Only {len(allegations)} key allegations (org min: 3).")
    if len(timeline) < 3:
        issues.append(f"Only {len(timeline)} timeline events (org min: 3).")
    if not entities:
        issues.append("No entities populated.")
    return _clamp(pts), issues


# Authoritative material types — the NGM equivalents of the old
# OFFICIAL_GOVERNMENT / LEGAL_* "strong" source types (ADR: cases own no
# documents). A case with at least one of these has an authoritative source.
# press_release stays here on purpose: a CIAA प्रेस विज्ञप्ति is a primary
# government source (the old OFFICIAL_GOVERNMENT) and counts as authoritative
# sourcing, even though it is NOT the charge sheet — the charge_sheet detector
# tracks the indictment separately, so a press-release-only case is still typed
# CIAA_BASIC. Do not drop it or press-release-only cases lose their sourcing.
_STRONG_MATERIAL_TYPES = {
    "CHARGE_SHEET",
    "PRESS_RELEASE",
    "COURT_ORDER",
    "OFFICIAL_REPORT",
    "LEGAL_CORPUS",
}


def sourcing(case):
    evidence = case.get("evidence") or []
    n = len(evidence)
    with_url = 0
    types = set()
    for ev in evidence:
        mat = ev.get("material") or {}
        if mat.get("urls"):
            with_url += 1
        mtype = mat.get("material_type")
        if mtype:
            types.add(mtype.upper())
    pts = 0
    pts += min(n / GOLD_SOURCES, 1.0) * 40
    pts += (with_url / n if n else 0) * 30
    strong = types & _STRONG_MATERIAL_TYPES
    pts += min(len(types) / 3.0, 1.0) * 15
    pts += 15 if strong else 0
    issues = []
    if n < 2:
        issues.append(f"Only {n} sources (org min: 2).")
    if n and with_url < n:
        issues.append(f"{n - with_url} of {n} sources lack a resolvable URL.")
    if not strong:
        issues.append(
            "No authoritative material (charge sheet / court order / official "
            "report / legal corpus / CIAA press release) present."
        )
    return _clamp(pts), issues


def ciaa_press_release(case):
    tt = _source_titles(case)
    present = any(
        st == "PRESS_RELEASE" or (_any(t, _PRESS_RELEASE) and _any(t, _CIAA))
        for t, st in tt
    )
    if present:
        return 100, []
    # any CIAA source at all is partial credit
    if any(_any(t, _CIAA) for t, _ in tt):
        return 55, [
            "A CIAA source is attached but not clearly a press release (प्रेस विज्ञप्ति)."
        ]
    return 0, ["No CIAA press release attached, yet the case is typed CIAA."]


def charge_sheet(case):
    tt = _source_titles(case)
    # Exclude anything explicitly typed press_release — a CIAA press release is
    # not the charge sheet (अभियोग पत्र), even if its title mentions the charge.
    has_cs = any(_any(t, _CHARGESHEET) and st != "PRESS_RELEASE" for t, st in tt)
    if has_cs:
        return 100, []
    return 0, ["No charge sheet (अभियोग पत्र) document among sources."]


def court_record(case):
    tt = _source_titles(case)
    has_cs = any(_any(t, _CHARGESHEET) and st != "PRESS_RELEASE" for t, st in tt)
    has_verdict = any(_any(t, _VERDICT) for t, _ in tt)
    has_order = any(_any(t, _COURT_ORDER) for t, _ in tt)
    pts = 0
    pts += 50 if has_cs else 0
    pts += 50 if (has_verdict or has_order) else 0
    issues = []
    if not has_cs:
        issues.append("No charge sheet (अभियोग पत्र) document among sources.")
    if not (has_verdict or has_order):
        issues.append("No special-court verdict (फैसला) or order (आदेश) among sources.")
    return _clamp(pts), issues


def timeline(case):
    tl = case.get("timeline") or []
    if not tl:
        return 0, ["No timeline provided."]
    n = len(tl)
    with_bs = sum(1 for t in tl if t.get("date_bs"))
    with_ad = sum(1 for t in tl if t.get("date"))
    detailed = sum(1 for t in tl if len((t.get("description") or "")) > 60)
    dates = [t.get("date") for t in tl if t.get("date")]
    ordered = dates == sorted(dates)
    pts = 0
    pts += min(n / GOLD_TIMELINE, 1.0) * 35
    pts += (with_ad / n) * 15
    pts += (with_bs / n) * 15
    pts += (detailed / n) * 25
    pts += 10 if ordered else 0
    issues = []
    if with_bs < n:
        issues.append(f"{n - with_bs} events missing Bikram Sambat (date_bs).")
    if not ordered:
        issues.append("Timeline events are not in chronological order.")
    if detailed < n:
        issues.append(f"{n - detailed} timeline events are thin (<60 chars).")
    return _clamp(pts), issues


def _entities_of_type(case, type_name):
    return [
        e
        for e in (case.get("entities") or [])
        if (e.get("type") or "").lower() == type_name
    ]


def accused_present(case):
    """At least one ACCUSED entity must be present (hard requirement).

    Only enforced for case types that require a named accused party (currently
    CORRUPTION). Other case types (e.g. TAX_EVASION) pass automatically. The
    policy lives in ``cases.models.requires_accused`` so the model, admin, and
    review engine stay in sync.
    """
    from cases.models import requires_accused

    if not requires_accused((case.get("case_type") or "").upper()):
        return 100, []
    accused = _entities_of_type(case, "accused")
    if not accused:
        return 0, ["No accused entity identified; at least one is required."]
    return 100, []


def related_entity_present(case):
    """At least one 'related' entity — anything that is neither accused nor location.

    'related (anything else)' covers every entity role except accused and
    location (in practice the `related` and `alleged` types).
    """
    entities = case.get("entities") or []
    others = [
        e
        for e in entities
        if (e.get("type") or "").lower() not in ("accused", "location")
    ]
    if not others:
        return 0, [
            "No related entities (anything other than accused/location); "
            "at least one is required."
        ]
    return 100, []


def location_entity_count(case):
    """Score the number of LOCATION entities on the case.

    A case should have 1-5 location entities, with 3 being the preferred count:
      - 3 locations           -> 100 (ideal)
      - 1, 2, 4 or 5 locations -> within the acceptable band, lightly penalised
                                  by distance from the preferred 3.
      - 0 locations            -> 0 (none identified)
      - more than 5 locations  -> over-tagged; penalised, floors at 40.
    """
    entities = case.get("entities") or []
    locations = [e for e in entities if (e.get("type") or "").lower() == "location"]
    n = len(locations)
    issues = []

    if n == 0:
        return 0, ["No location entities identified (1-5 expected, 3 preferred)."]
    if n > 5:
        # Over-tagged: start below the in-band edge (n=5 scores 70) and fall
        # 10 per extra location, never below 40. 6 -> 60, 7 -> 50, 8 -> 40.
        score = max(40, 70 - (n - 5) * 10)
        issues.append(
            f"{n} location entities; expected 1-5 (3 preferred). Likely over-tagged."
        )
        return _clamp(score), issues

    # 1..5 locations: full marks at the preferred 3, -15 per step away from it.
    score = 100 - abs(n - 3) * 15
    if n < 3:
        ent_word = "entity" if n == 1 else "entities"
        issues.append(
            f"Are you sure there {'is' if n == 1 else 'are'} only {n} location "
            f"{ent_word}? 3 are preferred."
        )
    return _clamp(score), issues


DETECTORS = {
    "court_case_number": court_case_number,
    "additional_description": additional_description,
    "structural_completeness": structural_completeness,
    "sourcing": sourcing,
    "ciaa_press_release": ciaa_press_release,
    "charge_sheet": charge_sheet,
    "court_record": court_record,
    "timeline": timeline,
    "court_number_in_title": court_number_in_title,
    "bigo_amount_present": bigo_amount_present,
    "accused_present": accused_present,
    "related_entity_present": related_entity_present,
    "location_entity_count": location_entity_count,
}


def is_applicable(rule_applies_to, case_type):
    if not rule_applies_to:
        return True
    if "ALL" in rule_applies_to:
        return True
    return case_type in rule_applies_to
