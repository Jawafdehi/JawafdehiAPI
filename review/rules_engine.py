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
        src = ev.get("source") or {}
        out.append(
            ((src.get("title") or "").lower(), (src.get("source_type") or "").upper())
        )
    return out


def _valid_link_roles():
    """The set of valid link-role values, sourced from the model enum.

    Lazily imported (like ``accused_present`` does for ``requires_accused``) so
    the model, admin, and review engine stay in sync — adding a value to
    ``SourceLinkRole`` automatically widens what the review gate accepts.
    """
    from cases.models import SourceLinkRole

    return {r.value for r in SourceLinkRole}


def _sources(case):
    """The source dicts behind a case's evidence, in order.

    One entry per evidence item (NOT de-duped): a source attached to several
    evidence rows is judged once per attachment, matching the other detectors
    (``_source_titles`` et al.) and the original ``sourcing`` source count.

    Each source dict carries the role-tagged ``urls`` ([{link, role}]) and the
    legacy flat ``url`` string list (see sourcing.jds_client.extract_sources).
    """
    return [ev.get("source") or {} for ev in (case.get("evidence") or [])]


def _source_roles(src):
    """Role list for a source's links. Returns ([roles], has_legacy_flat).

    A missing/``None`` role is coerced to ``RAW`` to match
    cases.models.normalize_url_list, so a legacy link without an explicit role
    is treated as the canonical document rather than an invalid one. A source
    still on the deprecated flat ``url`` shape (no role-tagged ``urls``) is
    reported as has_legacy_flat=True; detectors must not penalise that shape.
    """
    urls = src.get("urls") or []
    roles = [
        (u.get("role") if u.get("role") is not None else "RAW")
        for u in urls
        if isinstance(u, dict)
    ]
    has_legacy_flat = not urls and bool(src.get("url"))
    return roles, has_legacy_flat


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


# A case legitimately has no bigo when the charge sheet fixes no quantified sum
# (record/process offences under दफा ११/८, non-CIAA jurisdictions, or pre-charge
# allegations). Such cases certify that by recording a marker line in
# internal_notes, e.g. "NO_BIGO: record_offence — आरोपपत्रमा बिगो रकम उल्लेख छैन".
_NO_BIGO_MARKER = re.compile(r"(?im)^\s*no[_\s-]?bigo\b")


def _has_no_bigo_marker(internal_notes):
    """True if internal_notes certifies a legitimate no-bigo case via a NO_BIGO line."""
    return bool(internal_notes) and bool(_NO_BIGO_MARKER.search(str(internal_notes)))


def bigo_amount_present(case):
    """The bigo (बिगो) amount — total disputed/embezzled sum in NPR — must be set.

    Required for CIAA cases: the figure anchors the allegation. A missing or
    non-positive bigo fails the gate UNLESS the case certifies a legitimate
    no-bigo via a ``NO_BIGO:`` marker line in ``internal_notes`` (a rare, explicit
    opt-out for record/process offences, non-CIAA jurisdictions, or pre-charge
    allegations). A bare null still fails — that catches the common data gap.
    """
    bigo = case.get("bigo")
    if bigo is not None and bigo != "":
        try:
            value = int(bigo)
        except (TypeError, ValueError):
            return 0, [f"Bigo amount is not a valid number: {bigo!r}."]
        if value > 0:
            return 100, []
        if value < 0:
            return 0, [f"Bigo amount must not be negative (got {value})."]
        # value == 0 falls through to the no-bigo certification check below.

    if _has_no_bigo_marker(case.get("internal_notes")):
        return 100, []
    return 0, [
        "Bigo (बिगो) amount is not set and no NO_BIGO justification is recorded "
        "in internal_notes; it is required for CIAA cases."
    ]


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
    if len(allegations) < 2:
        issues.append(f"Only {len(allegations)} key allegations (org min: 2).")
    if len(timeline) < 3:
        issues.append(f"Only {len(timeline)} timeline events (org min: 3).")
    if not entities:
        issues.append("No entities populated.")
    return _clamp(pts), issues


def sourcing(case):
    sources = _sources(case)
    n = len(sources)
    with_raw = 0
    types = set()
    for src in sources:
        roles, has_legacy_flat = _source_roles(src)
        # A source is properly evidenced when it resolves to a canonical (RAW)
        # document. Legacy flat-`url` sources count as RAW (normalize_url_list).
        if "RAW" in roles or has_legacy_flat:
            with_raw += 1
        if src.get("source_type"):
            types.add(src["source_type"])
    pts = 0
    pts += min(n / GOLD_SOURCES, 1.0) * 40
    pts += (with_raw / n if n else 0) * 30
    strong = {t for t in types if t.startswith("OFFICIAL") or t.startswith("LEGAL")}
    pts += min(len(types) / 3.0, 1.0) * 15
    pts += 15 if strong else 0
    issues = []
    if n < 2:
        issues.append(f"Only {n} sources (org min: 2).")
    if n and with_raw < n:
        issues.append(
            f"{n - with_raw} of {n} sources have no canonical (RAW) document link."
        )
    if not strong:
        issues.append("No OFFICIAL_GOVERNMENT or LEGAL_* source type present.")
    return _clamp(pts), issues


def source_link_roles_valid(case):
    """Structural validity of every source's links (gate).

    Stricter than cases.models.validate_url_list: that validator only requires
    each link to carry a recognised role, whereas this gate additionally
    requires exactly one canonical RAW link per source and rejects link-less
    sources. A case fails when any source has: no links, a link with an
    unrecognised role, no RAW link, or more than one RAW link. Sources still on
    the legacy flat-`url` shape are exempt (normalize_url_list treats those
    links as a single RAW). Binary: 100 when every source is well-formed, else 0
    (so the gate rejects).
    """
    valid_roles = _valid_link_roles()
    issues = []
    for src in _sources(case):
        roles, has_legacy_flat = _source_roles(src)
        if has_legacy_flat:
            continue
        title = (src.get("title") or "?")[:40]
        if not roles:
            issues.append(f"'{title}': source has no links.")
            continue
        invalid = sorted({r for r in roles if r not in valid_roles})
        if invalid:
            issues.append(f"'{title}': invalid link role(s) {invalid}.")
        raw_count = roles.count("RAW")
        if raw_count == 0:
            issues.append(f"'{title}': no canonical (RAW) link.")
        elif raw_count > 1:
            issues.append(
                f"'{title}': {raw_count} RAW links; exactly one is allowed "
                "(extra copies should be ALTERNATE/MARKDOWN/PERMALINK)."
            )
    if issues:
        return 0, issues
    return 100, []


def ciaa_press_release(case):
    tt = _source_titles(case)
    present = any(
        (_any(t, _PRESS_RELEASE) and (_any(t, _CIAA) or st == "OFFICIAL_GOVERNMENT"))
        or (_any(t, _CIAA) and _any(t, _PRESS_RELEASE))
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
    has_cs = any(_any(t, _CHARGESHEET) for t, _ in tt)
    if has_cs:
        return 100, []
    return 0, ["No charge sheet (अभियोग पत्र) document among sources."]


def court_record(case):
    tt = _source_titles(case)
    has_cs = any(_any(t, _CHARGESHEET) for t, _ in tt)
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


DETECTORS = {
    "court_case_number": court_case_number,
    "additional_description": additional_description,
    "structural_completeness": structural_completeness,
    "sourcing": sourcing,
    "source_link_roles_valid": source_link_roles_valid,
    "ciaa_press_release": ciaa_press_release,
    "charge_sheet": charge_sheet,
    "court_record": court_record,
    "timeline": timeline,
    "court_number_in_title": court_number_in_title,
    "bigo_amount_present": bigo_amount_present,
    "accused_present": accused_present,
    "related_entity_present": related_entity_present,
}


def is_applicable(rule_applies_to, case_type):
    if not rule_applies_to:
        return True
    if "ALL" in rule_applies_to:
        return True
    return case_type in rule_applies_to
