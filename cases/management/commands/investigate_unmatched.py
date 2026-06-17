"""Investigate 68 unmatched NGM Special Court cases against Jawafdehi CORRUPTION dataset.

Read-only audit for JAWA-2760. Classifies unmatched cases into A (missing),
B (wrong ref), C (non-CIAA), or D (inconclusive). Uses internal NGM DB
connection — no external HTTP calls.
"""

import json
import logging
import os
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connections
from django.db.utils import DatabaseError

from ngm.services import (
    get_court_case_details,
    ensure_ngm_database_configured,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The 68 unclassified NGM case numbers (Group B from the completeness report)
# ---------------------------------------------------------------------------
GROUP_B_CASES = [
    # FY 071
    "071-CR-0214", "071-CR-0215", "071-CR-0299", "071-CR-0304", "071-CR-0318",
    # FY 072
    "072-CR-0002", "072-CR-0028", "072-CR-0035", "072-CR-0053", "072-CR-0056",
    "072-CR-0069", "072-CR-0081", "072-CR-0082", "072-CR-0089", "072-CR-0099",
    "072-CR-0101",
    # FY 073
    "073-CR-0010", "073-CR-0057", "073-CR-0066", "073-CR-0082", "073-CR-0137",
    # FY 074
    "074-CR-0025",
    # FY 075
    "075-CR-0134", "075-CR-0249", "075-CR-0304", "075-CR-0330", "075-CR-0331",
    "075-CR-0367",
    # FY 076
    "076-CR-0386",
    # FY 077
    "077-CR-0033",
    # FY 078
    "078-CR-0052", "078-CR-0106",
    # FY 079
    "079-CR-0046", "079-CR-0059", "079-CR-0083", "079-CR-0090", "079-CR-0093",
    "079-CR-0100", "079-CR-0115", "079-CR-0124", "079-CR-0153",
    # FY 080
    "080-CR-0099", "080-CR-0103", "080-CR-0104", "080-CR-0105",
    # FY 081
    "081-CR-0036", "081-CR-0040", "081-CR-0113", "081-CR-0131",
    # FY 082
    "082-CR-0053", "082-CR-0095", "082-CR-0098", "082-CR-0099", "082-CR-0100",
    "082-CR-0101", "082-CR-0102", "082-CR-0105", "082-CR-0106", "082-CR-0109",
    "082-CR-0119", "082-CR-0120", "082-CR-0131", "082-CR-0135", "082-CR-0139",
    "082-CR-0140", "082-CR-0141", "082-CR-0143", "082-CR-0144",
]

# ---------------------------------------------------------------------------
# Nepali keywords for CIAA relevance classification
# ---------------------------------------------------------------------------
CIAA_KEYWORDS = [
    "भ्रष्टाचार",           # corruption
    "सम्पत्ति शुद्धीकरण",   # money laundering
    "अख्तियार",             # CIAA (commission for investigation of abuse of authority)
    "CIAA",
]

NON_CIAA_KEYWORDS = [
    "वनमासी",               # forest offense
    "लागु औषध",             # narcotics
    "आतङककारी",             # terrorism
    "जिउ मास्ने वेच्ने",    # human trafficking
    "निर्णय वदर",           # decision nullification
    "विदेशी मुद्रा",        # foreign currency
    "कर चुहावट",            # tax evasion (non-CIAA)
    "हातहतियार",            # weapons
]


def classify_ciaa_relevance(case_details: dict | None) -> tuple[bool, str]:
    """Determine if a case is CIAA-related based on NGM case metadata.

    Returns (is_ciaa, reason).
    """
    if case_details is None:
        return False, "No NGM data available"

    case = case_details.get("case", {})
    case_type = (case.get("case_type") or "").lower()
    plaintiff = (case.get("plaintiff") or "").lower()
    defendant = (case.get("defendant") or "").lower()
    category = (case.get("category") or "").lower()

    combined_text = f"{case_type} {plaintiff} {defendant} {category}"

    # Check non-CIAA keywords first (stronger signal)
    for kw in NON_CIAA_KEYWORDS:
        if kw.lower() in combined_text:
            return False, f"Non-CIAA case_type/category: {case_type[:80]}"

    # Check CIAA keywords
    for kw in CIAA_KEYWORDS:
        if kw.lower() in combined_text:
            return True, f"CIAA keyword match: '{kw}' in case metadata"

    # Ambiguous — not clearly CIAA nor clearly non-CIAA
    return False, f"Ambiguous — case_type='{case_type}'"


def search_jawafdehi(case_number: str) -> dict | None:
    """Search Jawafdehi Case model for a matching court_cases reference.

    Returns the Case's case_id + slug if found, None otherwise.
    """
    from cases.models import Case

    # 1. Exact match in court_cases JSON field (format "special:{case_number}")
    exact_pattern = f"special:{case_number}"
    qs = Case.objects.filter(court_cases__contains=[exact_pattern])
    if qs.exists():
        c = qs.first()
        return {"case_id": c.case_id, "slug": c.slug, "match_type": "exact"}

    # 2. Try colon-prefixed match (some have "CIAA:" prefix instead of "special:")
    qs = Case.objects.filter(court_cases__contains=[f":{case_number}"])
    if qs.exists():
        c = qs.first()
        return {"case_id": c.case_id, "slug": c.slug, "match_type": "colon_suffix"}

    # 3. Try raw court_cases string contains (catches typos like O81/0 mixups)
    from django.db.models import Q
    qs = Case.objects.filter(
        Q(court_cases__icontains=case_number)
    )
    if qs.exists():
        c = qs.first()
        return {
            "case_id": c.case_id,
            "slug": c.slug,
            "match_type": "icontains_raw",
        }

    return None


def investigate_case(case_number: str, delay_s: float = 0.1) -> dict:
    """Investigate a single unmatched NGM case number.

    Returns an investigation result dict with category classification.
    """
    result = {
        "case_number": case_number,
        "ngm_case_type": None,
        "ngm_plaintiff": None,
        "ngm_defendant": None,
        "ngm_category": None,
        "ngm_status": None,
        "found_in_jawafdehi": False,
        "jawafdehi_match": None,
        "is_ciaa_case": False,
        "category": None,
        "category_reason": "",
        "recommended_action": "",
    }

    # ── Phase 2.1: Query NGM ──────────────────────────────────────────
    try:
        details = get_court_case_details("special", case_number)
    except (ValueError, DatabaseError) as exc:
        result["category"] = "D"
        result["category_reason"] = f"NGM query failed: {exc}"
        result["recommended_action"] = "Check NGM connection or retry"
        return result

    if details is None:
        result["category"] = "D"
        result["category_reason"] = "Not found in NGM court_cases table"
        result["recommended_action"] = "Manual verification — case number may be stale"
        return result

    case_data = details.get("case", {})
    result["ngm_case_type"] = case_data.get("case_type")
    result["ngm_plaintiff"] = case_data.get("plaintiff")
    result["ngm_defendant"] = case_data.get("defendant")
    result["ngm_category"] = case_data.get("category")
    result["ngm_status"] = case_data.get("status")

    # ── Phase 2.2: Classify CIAA relevance ────────────────────────────
    is_ciaa, reason = classify_ciaa_relevance(details)
    result["is_ciaa_case"] = is_ciaa

    # ── Phase 2.3: Search Jawafdehi ───────────────────────────────────
    match = search_jawafdehi(case_number)
    result["found_in_jawafdehi"] = match is not None
    result["jawafdehi_match"] = match

    # ── Phase 2.4: Assign Category ────────────────────────────────────
    if match:
        result["category"] = "B"
        result["category_reason"] = (
            f"Case exists in Jawafdehi (id={match['case_id']}) "
            f"but court_cases ref is absent/wrong (match_type={match['match_type']})"
        )
        result["recommended_action"] = (
            f"Add court_cases ref 'special:{case_number}' "
            f"to Case {match['case_id']}"
        )
    elif is_ciaa:
        result["category"] = "A"
        result["category_reason"] = (
            f"CIAA corruption case in NGM, not found in Jawafdehi: {reason}"
        )
        result["recommended_action"] = (
            f"Import case {case_number} into Jawafdehi from NGM data"
        )
    else:
        result["category"] = "C"
        result["category_reason"] = reason or "Non-CIAA case — legitimately absent"
        result["recommended_action"] = "Exclude from scope"

    return result


def investigate_typo_081() -> dict:
    """Verify the O81-CR-0095 typo."""
    from cases.models import Case
    from django.db.models import Q

    result = {
        "searched": "O81-CR-0095",
        "correct_value": "081-CR-0095",
        "found": False,
        "case_id": None,
        "details": None,
    }

    # Search for the typo variant
    qs = Case.objects.filter(
        Q(court_cases__icontains="O81-CR-0095")
    )
    if qs.exists():
        c = qs.first()
        result["found"] = True
        result["case_id"] = c.case_id
        result["details"] = {
            "slug": c.slug,
            "title": c.title,
            "current_court_cases": c.court_cases,
        }
    else:
        # Try matching the correct value too
        qs = Case.objects.filter(
            Q(court_cases__icontains="081-CR-0095")
        )
        if qs.exists():
            c = qs.first()
            result["found"] = False
            result["details"] = {
                "note": "Typo may already be fixed — correct ref found",
                "slug": c.slug,
                "current_court_cases": c.court_cases,
            }

    return result


def investigate_fy082_gap() -> dict:
    """Quantify the FY 082/83 gap between NGM and Jawafdehi."""
    from cases.models import Case
    from django.db.models import Q

    ensure_ngm_database_configured()

    # ── Query NGM for all 082-CR-* cases ──
    from ngm.services import ngm_read_connection

    try:
        with ngm_read_connection().cursor() as cursor:
            cursor.execute(
                """
                SELECT case_number, case_type, plaintiff, defendant, category, status
                FROM court_cases
                WHERE court_identifier = 'special'
                  AND case_number LIKE '082-CR-%'
                ORDER BY case_number
                """
            )
            ngm_rows = cursor.fetchall()
            ngm_columns = [col[0] for col in cursor.description]
    except DatabaseError as exc:
        return {"error": f"FY082 NGM query failed: {exc}", "ngm_cases": [], "jawafdehi_cases": [], "gap_cases": []}

    ngm_cases = [dict(zip(ngm_columns, row)) for row in ngm_rows]

    # ── Query Jawafdehi for all cases with 082-CR court_cases ──
    jawafdehi_cases = list(
        Case.objects.filter(
            Q(court_cases__icontains="082-CR")
        ).values("case_id", "slug", "court_cases")[:500]
    )

    # ── Build set of CR numbers in Jawafdehi ──
    jawafdehi_cr_numbers = set()
    for c in jawafdehi_cases:
        ccs = c.get("court_cases") or []
        if isinstance(ccs, list):
            for cc in ccs:
                if ":" in cc:
                    _, cn = cc.split(":", 1)
                    if cn.startswith("082-CR-"):
                        jawafdehi_cr_numbers.add(cn)

    # ── Diff: NGM cases not in Jawafdehi ──
    gap_cases = []
    for nc in ngm_cases:
        cn = nc["case_number"]
        if cn not in jawafdehi_cr_numbers:
            is_ciaa, reason = classify_ciaa_relevance({"case": nc})
            gap_cases.append({
                "case_number": cn,
                "case_type": nc.get("case_type"),
                "is_ciaa": is_ciaa,
                "classification_reason": reason,
            })

    return {
        "ngm_case_count": len(ngm_cases),
        "jawafdehi_case_count": len(jawafdehi_cases),
        "jawafdehi_cr_match_count": len(jawafdehi_cr_numbers),
        "gap_count": len(gap_cases),
        "gap_cases": gap_cases,
    }


class Command(BaseCommand):
    help = (
        "Investigate 68 unmatched NGM Special Court cases against the "
        "Jawafdehi CORRUPTION dataset. Read-only — no writes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only verify NGM connection and print case list, skip investigation",
        )
        parser.add_argument(
            "--output-dir",
            default="/tmp",
            help="Directory for output files (default: /tmp)",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=0.1,
            help="Delay (seconds) between NGM queries to avoid rate limits",
        )
        parser.add_argument(
            "--cases",
            type=str,
            nargs="*",
            help="Specific case numbers to investigate (default: all 68)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        output_dir = options["output_dir"]
        delay = options["delay"]
        specific_cases = options.get("cases")

        # ── Phase 1: Bootstrap ──
        self.stdout.write(self.style.NOTICE("=" * 60))
        self.stdout.write("CIAA Unmatched Case Investigation")
        self.stdout.write(f"Settings module: {os.environ.get('DJANGO_SETTINGS_MODULE', 'N/A')}")
        self.stdout.write(f"NGM DB configured: {'ngm' in settings.DATABASES}")
        self.stdout.write("=" * 60)

        # ── Verify NGM connection ──
        self.stdout.write("\n[CHECK] Verifying NGM database connection...")
        try:
            ensure_ngm_database_configured()
            self.stdout.write(self.style.SUCCESS("  ✓ NGM DB configured"))
        except ValueError as exc:
            self.stderr.write(self.style.ERROR(f"  ✗ {exc}"))
            self.stdout.write(self.style.WARNING(
                "\nCannot proceed without NGM DB. Check NGM_DATABASE_URL env var."
            ))
            return

        cases_to_investigate = specific_cases if specific_cases else GROUP_B_CASES
        self.stdout.write(f"\n[CHECK] Case count: {len(cases_to_investigate)}")

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    "\n✓ Dry-run checks passed. "
                    "NGM connection OK, case list loaded. "
                    "Re-run without --dry-run to investigate."
                )
            )
            return

        # ── Phase 2: Per-case Investigation ──
        self.stdout.write("\n[INVESTIGATE] Beginning per-case analysis...")
        self.stdout.write("-" * 60)

        findings = []
        categories = {"A": 0, "B": 0, "C": 0, "D": 0}

        for i, case_number in enumerate(cases_to_investigate, 1):
            self.stdout.write(f"  [{i}/{len(cases_to_investigate)}] {case_number}...", ending=" ")
            self.stdout.flush()

            result = investigate_case(case_number, delay)
            findings.append(result)

            cat = result["category"]
            categories[cat] = categories.get(cat, 0) + 1
            self.stdout.write(f"→ Category {cat}")

            if delay > 0:
                time.sleep(delay)

        # ── Phase 3: Typo Verification ──
        self.stdout.write("\n[TYPO] Verifying O81-CR-0095...")
        typo_result = investigate_typo_081()

        # ── Phase 4: FY 082 Gap ──
        self.stdout.write("\n[FY082] Analyzing FY 082/83 gap...")
        fy082_result = investigate_fy082_gap()

        # ── Phase 5: Reporting ──
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write("[REPORT] Generating output files...")
        self._generate_report(
            findings, categories, typo_result, fy082_result, output_dir
        )

    def _generate_report(self, findings, categories, typo, fy082, output_dir):
        """Write JSON report and markdown summary."""
        report = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "issue": "JAWA-2760",
            "summary": {
                "total_investigated": len(findings),
                "category_A_missing_ciaa": categories.get("A", 0),
                "category_B_wrong_ref": categories.get("B", 0),
                "category_C_non_ciaa": categories.get("C", 0),
                "category_D_inconclusive": categories.get("D", 0),
                "typo": typo,
                "fy082": fy082,
            },
            "group_b_findings": findings,
            "typo_finding": typo,
            "fy082_findings": fy082,
        }

        json_path = os.path.join(output_dir, "ciaa_investigation_report.json")
        md_path = os.path.join(output_dir, "ciaa_investigation_summary.md")

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        self.stdout.write(self.style.SUCCESS(f"  ✓ JSON report: {json_path}"))

        self._write_markdown_summary(md_path, report)
        self.stdout.write(self.style.SUCCESS(f"  ✓ Markdown summary: {md_path}"))

        # ── Summary table to stdout ──
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write("INVESTIGATION SUMMARY")
        self.stdout.write("=" * 60)
        s = report["summary"]
        self.stdout.write(f"  Total investigated:   {s['total_investigated']}")
        self.stdout.write(f"  Cat A (missing):      {s['category_A_missing_ciaa']}")
        self.stdout.write(f"  Cat B (wrong ref):    {s['category_B_wrong_ref']}")
        self.stdout.write(f"  Cat C (non-CIAA):     {s['category_C_non_ciaa']}")
        self.stdout.write(f"  Cat D (inconclusive): {s['category_D_inconclusive']}")
        self.stdout.write(f"  Typo confirmed:       {typo.get('found', False)}")
        self.stdout.write(f"  FY 082 gap cases:     {fy082.get('gap_count', '?')}")

    def _write_markdown_summary(self, path, report):
        """Write human-readable markdown summary."""
        s = report["summary"]
        lines = [
            f"# CIAA Unmatched Case Investigation — Summary",
            f"",
            f"**Generated:** {report['generated_at']}  ",
            f"**Issue:** [JAWA-2760](/JAWA/issues/JAWA-2760)  ",
            f"",
            f"## Overview",
            f"",
            f"Investigated **{s['total_investigated']}** unmatched NGM case numbers.",
            f"",
            f"| Category | Count | Meaning |",
            f"|----------|-------|---------|",
            f"| **A** — Missing CIAA | {s['category_A_missing_ciaa']} | Genuine CIAA corruption case missing from Jawafdehi |",
            f"| **B** — Wrong ref | {s['category_B_wrong_ref']} | Case exists in Jawafdehi but court_cases ref missing/wrong |",
            f"| **C** — Non-CIAA | {s['category_C_non_ciaa']} | Legitimately excluded (forest, narcotics, etc.) |",
            f"| **D** — Inconclusive | {s['category_D_inconclusive']} | Needs human review |",
            f"",
            f"## Typo Verification",
            f"",
            f"- **O81-CR-0095** → `081-CR-0095`: **{'Confirmed' if typo.get('found') else 'Not found'}**",
            f"- Details: {json.dumps(typo, ensure_ascii=False, default=str)}",
            f"",
            f"## FY 082/83 Gap",
            f"",
            f"- NGM cases: {fy082.get('ngm_case_count', '?')}",
            f"- Jawafdehi CR matches: {fy082.get('jawafdehi_cr_match_count', '?')}",
            f"- Gap: **{fy082.get('gap_count', '?')}** cases present in NGM but not in Jawafdehi",
            f"",
        ]

        # Gap cases table
        gap_cases = fy082.get("gap_cases", [])
        if gap_cases:
            lines.append("| Case Number | Case Type | CIAA? | Reasoning |")
            lines.append("|------------|-----------|-------|-----------|")
            for gc in gap_cases:
                lines.append(
                    f"| {gc['case_number']} | {gc.get('case_type','?')[:40]} | "
                    f"{'Yes' if gc['is_ciaa'] else 'No'} | {gc.get('classification_reason','')} |"
                )
            lines.append("")

        # Category A table
        a_cases = [f for f in report["group_b_findings"] if f["category"] == "A"]
        if a_cases:
            lines.append("## Category A — Missing CIAA Cases (Import Needed)")
            lines.append("")
            lines.append("| Case Number | Defendant | Case Type |")
            lines.append("|------------|-----------|-----------|")
            for f in a_cases:
                lines.append(
                    f"| {f['case_number']} | {f.get('ngm_defendant','')[:50]} | "
                    f"{f.get('ngm_case_type','')[:50]} |"
                )
            lines.append("")

        # Category B table
        b_cases = [f for f in report["group_b_findings"] if f["category"] == "B"]
        if b_cases:
            lines.append("## Category B — Wrong/Missing court_cases Ref")
            lines.append("")
            lines.append("| Case Number | Jawafdehi ID | Match Type |")
            lines.append("|------------|--------------|------------|")
            for f in b_cases:
                m = f.get("jawafdehi_match") or {}
                lines.append(
                    f"| {f['case_number']} | {m.get('case_id','?')} | "
                    f"{m.get('match_type','?')} |"
                )
            lines.append("")

        # Category D table
        d_cases = [f for f in report["group_b_findings"] if f["category"] == "D"]
        if d_cases:
            lines.append("## Category D — Inconclusive (Needs Human Review)")
            lines.append("")
            lines.append("| Case Number | Reason |")
            lines.append("|------------|--------|")
            for f in d_cases:
                lines.append(f"| {f['case_number']} | {f.get('category_reason','?')} |")
            lines.append("")

        lines.extend([
            "---",
            "*Generated by JAWA-2762 investigation tool*",
        ])

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
