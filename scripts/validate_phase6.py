#!/usr/bin/env python3
"""
Phase 6: Validation & Quality Control

Cross-checks totals vs CIAA published statistics, FY distribution analysis,
entity consistency, case number format validation, date sanity checks,
coverage matrix generation, quality tier classification.

Outputs: validation-report.json, coverage-matrix.csv
"""

import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

DATA_DIR = Path(
    os.environ.get("CORRUPTION_DB_DIR", "/paperspace/tmp/corruption-case-db")
)
EXTRACTED_DIR = DATA_DIR / "extracted-data"
CONSOLIDATED_DIR = EXTRACTED_DIR / "consolidated"
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", Path(__file__).resolve().parent))

# ── Load all data sources ──────────────────────────────────────────────────


def load_json(path, label):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  WARN: {label} — {e}")
        return None


def load_all():
    print("Loading data sources...")
    sources = {}

    sources["enriched_cases"] = (
        load_json(
            EXTRACTED_DIR / "enriched-ciaa-cases.json", "enriched-ciaa-cases.json"
        )
        or []
    )

    sources["unified_cases"] = (
        load_json(EXTRACTED_DIR / "unified-ciaa-cases.json", "unified-ciaa-cases.json")
        or []
    )

    sources["cross_ref"] = (
        load_json(
            CONSOLIDATED_DIR / "cross-reference-details.json",
            "cross-reference-details.json",
        )
        or []
    )

    sources["annual_report"] = (
        load_json(
            EXTRACTED_DIR / "ciaa-annual-report-cases.json",
            "ciaa-annual-report-cases.json",
        )
        or {}
    )

    sources["ngm_cases"] = load_json(
        DATA_DIR / "ngm-special-court-cases.json", "ngm-special-court-cases.json"
    ) or {"cases": []}

    # entities_raw has entity data
    sources["entities_raw"] = (
        load_json(EXTRACTED_DIR / "entities_raw.json", "entities_raw.json") or []
    )

    print(f"  enriched-ciaa-cases.json:   {len(sources['enriched_cases'])} records")
    print(f"  unified-ciaa-cases.json:    {len(sources['unified_cases'])} records")
    print(f"  cross-reference-details:    {len(sources['cross_ref'])} records")
    print(
        f"  ciaa-annual-report-cases:   {len(sources['annual_report'].get('fiscal_year_stats', {}))} FY stats"
    )
    print(
        f"  ngm-special-court-cases:    {len(sources['ngm_cases'].get('cases', []))} cases"
    )
    print(f"  entities_raw.json:          {len(sources['entities_raw'])} entities")

    return sources


# ── 1. Case Number Format Validation ──────────────────────────────────────


def validate_case_numbers(sources):
    print("\n═══ 1. Case Number Format Validation ═══")
    issues = []
    patterns_seen = Counter()

    UNIFIED_FMT = re.compile(r"^(\d{2,3})-CR-(\d+)$")

    for r in sources["unified_cases"]:
        cn = r.get("case_number") or ""
        fy = r.get("fy", "???")

        if not cn.strip():
            issues.append(
                {
                    "severity": "error",
                    "check": "case_number_empty",
                    "fy": fy,
                    "value": cn,
                    "detail": "Empty case_number",
                }
            )
            patterns_seen["(empty)"] += 1
            continue

        m = UNIFIED_FMT.match(cn)
        if m:
            yr_part = int(m.group(1))
            # Year prefix should be plausible: 60-82 for BS years 2060-2082
            if yr_part < 40 or yr_part > 99:
                issues.append(
                    {
                        "severity": "warning",
                        "check": "case_number_year_outlier",
                        "fy": fy,
                        "value": cn,
                        "detail": f"Year prefix {yr_part} outside typical range 40-99",
                    }
                )
            patterns_seen["XX-CR-NNNN"] += 1
        else:
            issues.append(
                {
                    "severity": "warning",
                    "check": "case_number_format",
                    "fy": fy,
                    "value": cn,
                    "detail": "Does not match XX-CR-NNNN pattern",
                }
            )
            patterns_seen[cn[:30]] += 1

    # Check cross-ref cases
    for r in sources["cross_ref"]:
        cn = r.get("case_number", "")
        if cn and not UNIFIED_FMT.match(cn):
            issues.append(
                {
                    "severity": "warning",
                    "check": "xref_case_number_format",
                    "fy": r.get("fy", "???"),
                    "value": cn,
                    "detail": "Cross-ref case number doesn't match XX-CR-NNNN",
                }
            )

    print(f"  Patterns: {dict(patterns_seen.most_common(5))}")
    print(f"  Issues: {len(issues)}")
    return issues


# ── 2. Date Sanity Checks ─────────────────────────────────────────────────


def validate_dates(sources):
    print("\n═══ 2. Date Sanity Checks ═══")
    issues = []

    BS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    AD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    for r in sources["enriched_cases"]:
        fy = r.get("fy", "???")
        cn = r.get("case_number", "???")

        # Check BS dates
        for field in ["filing_date_bs", "verdict_date_bs", "verdict_date_bs_extracted"]:
            val = r.get(field)
            if val and isinstance(val, str):
                if not BS_RE.match(val):
                    issues.append(
                        {
                            "severity": "error",
                            "check": f"bad_bs_date_{field}",
                            "fy": fy,
                            "case_number": cn,
                            "value": val,
                            "detail": f"{field} doesn't match YYYY-MM-DD",
                        }
                    )
                else:
                    parts = val.split("-")
                    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
                    if y < 2040 or y > 2090:
                        issues.append(
                            {
                                "severity": "error",
                                "check": "bs_date_year_outlier",
                                "fy": fy,
                                "case_number": cn,
                                "value": val,
                                "detail": f"{field} year {y} outside BS range 2040-2090",
                            }
                        )
                    if m < 1 or m > 12:
                        issues.append(
                            {
                                "severity": "error",
                                "check": "bs_date_month_invalid",
                                "fy": fy,
                                "case_number": cn,
                                "value": val,
                                "detail": f"{field} month {m} invalid",
                            }
                        )
                    if d < 1 or d > 32:
                        issues.append(
                            {
                                "severity": "error",
                                "check": "bs_date_day_invalid",
                                "fy": fy,
                                "case_number": cn,
                                "value": val,
                                "detail": f"{field} day {d} invalid",
                            }
                        )

        # Check AD dates
        ad_val = r.get("filing_date_ad")
        if ad_val and isinstance(ad_val, str) and AD_RE.match(ad_val):
            y = int(ad_val.split("-")[0])
            if y < 2000 or y > 2030:
                issues.append(
                    {
                        "severity": "warning",
                        "check": "ad_date_year_outlier",
                        "fy": fy,
                        "case_number": cn,
                        "value": ad_val,
                        "detail": f"filing_date_ad year {y} outside expected range 2000-2030",
                    }
                )

        # Filing date should precede verdict date
        fd_bs = r.get("filing_date_bs")
        vd_bs = r.get("verdict_date_bs")
        if fd_bs and vd_bs and BS_RE.match(fd_bs) and BS_RE.match(vd_bs):
            if vd_bs < fd_bs:
                issues.append(
                    {
                        "severity": "error",
                        "check": "verdict_before_filing",
                        "fy": fy,
                        "case_number": cn,
                        "value": f"filed={fd_bs} verdict={vd_bs}",
                        "detail": "Verdict date precedes filing date",
                    }
                )

    # Check verdict_date_ad field which appears to contain raw text
    ad_raw = Counter()
    for r in sources["enriched_cases"]:
        vda = r.get("verdict_date_ad", "")
        if vda and not AD_RE.match(str(vda)):
            ad_raw[str(vda)[:40]] += 1

    if ad_raw:
        issues.append(
            {
                "severity": "info",
                "check": "verdict_date_ad_nonstandard",
                "fy": "multiple",
                "case_number": "multiple",
                "value": str(dict(ad_raw.most_common(5))),
                "detail": "verdict_date_ad has non-AD values (likely raw BS text)",
            }
        )

    print(f"  Issues: {len(issues)}")
    return issues


# ── 3. Entity Consistency Check ───────────────────────────────────────────


def extract_entity_variants(sources):
    print("\n═══ 3. Entity Consistency Check ═══")
    issues = []

    # Collect all entity names across all sources
    all_names = defaultdict(list)  # normalized -> [(original, source, fy, case_number)]

    def norm(name):
        """Normalize for comparison: lowercase, remove spaces, common prefixes."""
        if not name:
            return None
        n = name.strip().lower()
        n = re.sub(r"\s+", "", n)
        n = re.sub(r"[\.\,\-]", "", n)
        # Remove common Nepali honorifics/prefixes
        n = re.sub(r"^(श्री|स्वर्गीय|श्रीमती|श्रीयुत)", "", n)
        return n

    # From enriched cases — defendant/plaintiff fields
    for r in sources["enriched_cases"]:
        fy = r.get("fy", "???")
        cn = r.get("case_number", "???")
        for field in ["defendant", "plaintiff"]:
            val = r.get(field)
            if val and isinstance(val, str) and val.strip():
                key = norm(val)
                if key and len(key) >= 3:
                    all_names[key].append((val.strip(), "enriched", fy, cn, field))

        # From ngm_entities
        entities = r.get("ngm_entities", {})
        if isinstance(entities, dict):
            for role, names_list in entities.items():
                if isinstance(names_list, list):
                    for e in names_list:
                        if isinstance(e, dict):
                            name = e.get("name", e.get("full_name", ""))
                        elif isinstance(e, str):
                            name = e
                        else:
                            continue
                        if name and name.strip():
                            key = norm(name)
                            if key and len(key) >= 3:
                                all_names[key].append(
                                    (name.strip(), "ngm_entity", fy, cn, role)
                                )

    # From unified cases — defendants list
    for r in sources["unified_cases"]:
        fy = r.get("fy", "???")
        cn = r.get("case_number", "???")
        defendants = r.get("defendants", [])
        if isinstance(defendants, list):
            for d in defendants:
                if isinstance(d, dict):
                    name = d.get("name", d.get("full_name", ""))
                elif isinstance(d, str):
                    name = d
                else:
                    continue
                if name and name.strip():
                    key = norm(name)
                    if key and len(key) >= 3:
                        all_names[key].append(
                            (name.strip(), "unified", fy, cn, "defendant")
                        )

    # Find variants: same normal form but different display strings
    variant_count = 0
    for key, entries in all_names.items():
        originals = set(e[0] for e in entries)
        if len(originals) > 1:
            variant_count += 1
            if variant_count <= 30:  # Limit output
                # Show the most common variant
                counts = Counter(e[0] for e in entries)
                most_common = counts.most_common()
                issues.append(
                    {
                        "severity": "warning",
                        "check": "entity_spelling_variant",
                        "fy": "multiple",
                        "value": f"{most_common[0][0]} ({most_common[0][1]}x) vs {len(originals)-1} other form(s)",
                        "detail": f"Normalized key '{key}' has {len(originals)} display variants: {[(n, c) for n, c in most_common[:5]]}",
                        "sample_cases": list(set(e[2] for e in entries))[:5],
                    }
                )

    total_names = len(all_names)
    print(f"  Unique normalized names: {total_names}")
    print(f"  Variants (same entity, diff spelling): {variant_count}")
    print(f"  Reported: {min(30, variant_count)}")

    return issues


# ── 4. FY Distribution Analysis ───────────────────────────────────────────


def analyze_fy_distribution(sources):
    print("\n═══ 4. FY Distribution Analysis ═══")
    issues = []

    # Count cases per FY from each source
    enriched_fy = Counter(r.get("fy") for r in sources["enriched_cases"])
    unified_fy = Counter(r.get("fy") for r in sources["unified_cases"])
    cross_ref_fy = Counter(r.get("fy") for r in sources["cross_ref"])

    # Reference: CIAA published stats
    annual_stats = sources["annual_report"].get("fiscal_year_stats", {})

    print(f"  {'FY':<12} {'Enriched':>10} {'Unified':>10} {'XRef':>8} {'CIAA Pub':>10}")
    print(f"  {'-'*50}")

    all_fys = sorted(
        set(enriched_fy.keys())
        | set(unified_fy.keys())
        | set(cross_ref_fy.keys())
        | set(annual_stats.keys())
    )

    for fy in all_fys:
        # Normalize FY key (some use underscore, some slash)
        e_cnt = enriched_fy.get(fy, 0)
        u_cnt = unified_fy.get(fy, 0)
        x_cnt = cross_ref_fy.get(fy, 0)

        # Try matching CIAA stats
        fy_underscore = fy.replace("/", "_")
        ciaa_filed = None
        if fy_underscore in annual_stats:
            ciaa_filed = annual_stats[fy_underscore].get("cases_filed_special_court")

        ciaa_str = str(ciaa_filed) if ciaa_filed else "-"
        print(f"  {fy:<12} {e_cnt:>10} {u_cnt:>10} {x_cnt:>8} {ciaa_str:>10}")

        # Check for large discrepancies
        if ciaa_filed and u_cnt > 0:
            ratio = u_cnt / ciaa_filed
            if ratio > 2.0:
                issues.append(
                    {
                        "severity": "warning",
                        "check": "fy_case_count_exceeds_ciaa",
                        "fy": fy,
                        "value": f"unified={u_cnt} ciaa_published={ciaa_filed}",
                        "detail": f"Unified case count ({u_cnt}) > 2x CIAA published ({ciaa_filed})",
                    }
                )
            elif u_cnt < ciaa_filed * 0.3 and u_cnt > 0:
                issues.append(
                    {
                        "severity": "info",
                        "check": "fy_case_count_low_vs_ciaa",
                        "fy": fy,
                        "value": f"unified={u_cnt} ciaa_published={ciaa_filed}",
                        "detail": f"Unified case count ({u_cnt}) < 30% of CIAA published ({ciaa_filed})",
                    }
                )

    print(f"  Issues: {len(issues)}")
    return issues


# ── 5. Source Coverage Matrix ─────────────────────────────────────────────


def coverage_matrix(sources):
    print("\n═══ 5. Coverage Matrix ═══")

    rows = []

    # Enriched cases per FY per source
    fy_source = defaultdict(lambda: Counter())
    for r in sources["enriched_cases"]:
        fy = r.get("fy", "unknown")
        src = r.get("source", "unknown")
        fy_source[fy][src] += 1

    for fy in sorted(fy_source.keys()):
        src_counts = fy_source[fy]
        total = sum(src_counts.values())
        # Count distinct coverage buckets
        coverage_groups = {
            "ngm_only": src_counts.get("ngm_only", 0),
            "ngm_annual_report": sum(
                src_counts.get(k, 0) for k in ["ngm_annual_report", "all_three"]
            ),
            "annual_report_only": src_counts.get("annual_report_only", 0),
            "sheets": sum(
                src_counts.get(k, 0)
                for k in ["sheets_only", "sheets_annual_report", "both"]
            ),
        }

        # Source labels with counts
        ngm_label = f"NGM:{coverage_groups['ngm_only']}"
        ar_label = f"AR:{coverage_groups['ngm_annual_report'] + coverage_groups['annual_report_only']}"
        ss_label = f"SS:{coverage_groups['sheets']}"

        # Look up CIAA published figure
        fy_u = fy.replace("/", "_")
        ciaa_filed = None
        ar_stats = sources["annual_report"].get("fiscal_year_stats", {})
        if fy_u in ar_stats:
            ciaa_filed = ar_stats[fy_u].get("cases_filed_special_court")

        has_ngm = (
            "Y"
            if coverage_groups["ngm_only"] or coverage_groups["ngm_annual_report"]
            else "N"
        )
        has_ar = (
            "Y"
            if coverage_groups["ngm_annual_report"]
            or coverage_groups["annual_report_only"]
            else "N"
        )
        has_ss = "Y" if coverage_groups["sheets"] else "N"

        # CIAA match: if we have CIAA published figure, check coverage ratio
        coverage_pct = (
            round(total / ciaa_filed * 100, 1)
            if ciaa_filed and ciaa_filed > 0
            else None
        )

        rows.append(
            {
                "fy": fy,
                "total_cases": total,
                "ciaa_published": ciaa_filed or "-",
                "coverage_pct": coverage_pct if coverage_pct is not None else "-",
                "ngm_only": coverage_groups["ngm_only"],
                "ngm_annual_report": coverage_groups["ngm_annual_report"],
                "annual_report_only": coverage_groups["annual_report_only"],
                "sheets": coverage_groups["sheets"],
                "has_ngm": has_ngm,
                "has_ar": has_ar,
                "has_ss": has_ss,
                "sources_summary": f"{ngm_label} {ar_label} {ss_label}",
            }
        )

    # Write CSV
    csv_path = OUTPUT_DIR / "coverage-matrix.csv"
    fieldnames = [
        "fy",
        "total_cases",
        "ciaa_published",
        "coverage_pct",
        "ngm_only",
        "ngm_annual_report",
        "annual_report_only",
        "sheets",
        "has_ngm",
        "has_ar",
        "has_ss",
        "sources_summary",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"  FYs covered: {len(rows)}")
    print(f"  CSV: {csv_path}")
    return rows


# ── 6. Quality Tier Classification ────────────────────────────────────────


def classify_quality_tiers(sources):
    print("\n═══ 6. Quality Tier Classification ═══")

    tiers = {"Gold": [], "Silver": [], "Bronze": []}

    for r in sources["enriched_cases"]:
        scores = []

        # Case number present and valid
        cn = r.get("case_number", "")
        scores.append(1 if cn and re.match(r"^\d{2,3}-CR-\d+", cn) else 0)

        # Filing date present
        scores.append(1 if r.get("filing_date_bs") or r.get("filing_date_ad") else 0)

        # Verdict date present
        scores.append(1 if r.get("verdict_date_bs") or r.get("verdict_date_ad") else 0)

        # Defendant name present
        scores.append(1 if r.get("defendant") else 0)

        # Plaintiff name present
        scores.append(1 if r.get("plaintiff") else 0)

        # NGM entities populated
        entities = r.get("ngm_entities", {})
        scores.append(1 if isinstance(entities, dict) and len(entities) > 0 else 0)

        # Hearing count available
        hc = r.get("hearing_count")
        scores.append(1 if hc is not None and hc > 0 else 0)

        # Verdict judge present
        scores.append(1 if r.get("verdict_judge") else 0)

        # Source cross-referenced (not ngm_only)
        src = r.get("source", "")
        scores.append(1 if src != "ngm_only" and src != "unknown" else 0)

        total_score = sum(scores)
        max_score = len(scores)

        GOLD_TIER_THRESHOLD = 7
        SILVER_TIER_THRESHOLD = 4
        if total_score >= GOLD_TIER_THRESHOLD:
            tier = "Gold"
        elif total_score >= SILVER_TIER_THRESHOLD:
            tier = "Silver"
        else:
            tier = "Bronze"

        tiers[tier].append(
            {
                "case_number": cn,
                "fy": r.get("fy", "???"),
                "source": src,
                "score": total_score,
                "max_score": max_score,
                "details": {
                    "has_case_number": bool(scores[0]),
                    "has_filing_date": bool(scores[1]),
                    "has_verdict_date": bool(scores[2]),
                    "has_defendant": bool(scores[3]),
                    "has_plaintiff": bool(scores[4]),
                    "has_entities": bool(scores[5]),
                    "has_hearings": bool(scores[6]),
                    "has_judge": bool(scores[7]),
                    "cross_referenced": bool(scores[8]),
                },
            }
        )

    denom = max(len(sources["enriched_cases"]), 1)
    print(f"  Gold:   {len(tiers['Gold'])} ({len(tiers['Gold'])/denom*100:.1f}%)")
    print(f"  Silver: {len(tiers['Silver'])} ({len(tiers['Silver'])/denom*100:.1f}%)")
    print(f"  Bronze: {len(tiers['Bronze'])} ({len(tiers['Bronze'])/denom*100:.1f}%)")
    print(f"  Total:  {sum(len(v) for v in tiers.values())}")
    return tiers


# ── 7. Cross-Reference Validation ─────────────────────────────────────────


def validate_cross_reference(sources):
    print("\n═══ 7. Cross-Reference Validation ═══")
    issues = []

    xref = sources["cross_ref"]
    enriched = sources["enriched_cases"]

    enriched_case_nums = set(
        r.get("case_number") for r in enriched if r.get("case_number")
    )

    orphaned_xref = 0
    for r in xref:
        cn = r.get("case_number", "")
        if cn and cn not in enriched_case_nums:
            orphaned_xref += 1

    if orphaned_xref:
        issues.append(
            {
                "severity": "warning",
                "check": "orphaned_cross_ref",
                "fy": "multiple",
                "value": str(orphaned_xref),
                "detail": f"{orphaned_xref} cross-reference records have no matching enriched case",
            }
        )

    # Check for enriched cases with source=all_three but not in cross_ref
    xref_case_nums = set(r.get("case_number") for r in xref if r.get("case_number"))
    enriched_in_xref = sum(1 for cn in enriched_case_nums if cn in xref_case_nums)
    print(
        f"  Enriched cases in cross-ref: {enriched_in_xref}/{len(enriched_case_nums)}"
    )
    print(f"  Orphaned cross-refs: {orphaned_xref}")
    print(f"  Issues: {len(issues)}")
    return issues


# ── Main Pipeline ─────────────────────────────────────────────────────────


def main():
    sources = load_all()
    all_issues = []

    all_issues += validate_case_numbers(sources)
    all_issues += validate_dates(sources)
    all_issues += extract_entity_variants(sources)
    all_issues += analyze_fy_distribution(sources)
    matrix_rows = coverage_matrix(sources)
    tiers = classify_quality_tiers(sources)
    all_issues += validate_cross_reference(sources)

    # ── Build Report ──────────────────────────────────────────────────────
    report = {
        "pipeline": "Phase 6: Validation & Quality Control",
        "generated_at": date.today().isoformat(),
        "data_sources": {
            k: (
                len(v)
                if isinstance(v, list)
                else (
                    len(v.get("fiscal_year_stats", {})) if isinstance(v, dict) else "?"
                )
            )
            for k, v in sources.items()
        },
        "total_issues": len(all_issues),
        "issues": all_issues,
        "coverage_matrix": matrix_rows,
        "quality_tiers": {
            "gold_count": len(tiers["Gold"]),
            "silver_count": len(tiers["Silver"]),
            "bronze_count": len(tiers["Bronze"]),
            "gold_pct": round(
                len(tiers["Gold"]) / max(len(sources["enriched_cases"]), 1) * 100, 1
            ),
            "silver_pct": round(
                len(tiers["Silver"]) / max(len(sources["enriched_cases"]), 1) * 100, 1
            ),
            "bronze_pct": round(
                len(tiers["Bronze"]) / max(len(sources["enriched_cases"]), 1) * 100, 1
            ),
        },
        "summary": {
            "total_enriched_cases": len(sources["enriched_cases"]),
            "total_unified_cases": len(sources["unified_cases"]),
            "total_cross_ref_records": len(sources["cross_ref"]),
            "fiscal_years_in_stats": len(
                sources["annual_report"].get("fiscal_year_stats", {})
            ),
            "quality_issues_found": len(all_issues),
        },
    }

    # Write validation report
    report_path = OUTPUT_DIR / "validation-report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print("\n═══ Validation Report ═══")
    print(f"  Report: {report_path}")
    print(f"  Total issues: {len(all_issues)}")

    # Severity breakdown
    severity_counts = Counter(i["severity"] for i in all_issues)
    for sev, cnt in severity_counts.most_common():
        print(f"    {sev}: {cnt}")

    return report


if __name__ == "__main__":
    report = main()
    if report.get("total_issues", 0) > 0:
        sys.exit(1)
    sys.exit(0)
