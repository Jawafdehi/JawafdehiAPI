"""Tests for the read-only ``audit_jawafdehi_duplicates`` management command.

The command scans ``/material/jawafdehi/*`` materials, matches each against the
canonical corpus by natural key (via ``materials.dedup``), and writes a JSONL
report + a stdout summary. It MUTATES NOTHING. These run on sqlite.
See docs/superpowers/specs/2026-07-14-jawafdehi-dedup-audit-design.md.
"""

from __future__ import annotations

import io
import json

import pytest
from django.core.management import call_command

from cases.models import Case, CaseMaterialReference, CaseState, CaseType
from materials.models import Material

pytestmark = pytest.mark.django_db(databases=["default", "ngm"])

BASE = "https://jawafdehi.org"


def _jawaf(ident, source_type, name, material_type="document"):
    iri = f"{BASE}/material/jawafdehi/{ident}"
    return Material.objects.create(
        iri=iri,
        material_type=material_type,
        source="jawafdehi",
        ident=ident,
        data={
            "@id": iri,
            "@type": "DigitalDocument",
            "jawafdehi:sourceType": source_type,
            "name": {"ne": name},
        },
    )


def _canonical(source, ident, material_type="document"):
    iri = f"{BASE}/material/{source}/{ident}"
    return Material.objects.create(
        iri=iri, material_type=material_type, source=source, ident=ident,
        data={"@id": iri, "@type": "CreativeWork", "name": {"ne": "canonical"}},
    )


def _run(tmp_path):
    out = tmp_path / "report.jsonl"
    stdout = io.StringIO()
    call_command("audit_jawafdehi_duplicates", "--output", str(out), stdout=stdout)
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    return {r["jawafdehi_iri"]: r for r in rows}, stdout.getvalue()


def test_press_release_with_canonical_is_a_duplicate(tmp_path):
    m = _jawaf("20260507.pr", "CIAA_PRESS_RELEASE", "CIAA प्रेस विज्ञप्ति नं. ३१५५ — आरोपपत्र")
    _canonical("ciaa_press_release", "3155")
    report, summary = _run(tmp_path)
    row = report[m.iri]
    assert row["outcome"] == "duplicate"
    assert row["canonical_iri"] == f"{BASE}/material/ciaa_press_release/3155"
    assert "3155" in row["signal"]
    # The summary reports the headline duplicate count.
    assert "duplicate a document we already hold" in summary


def test_court_order_matches_canonical_by_case_number_suffix(tmp_path):
    m = _jawaf("20260507.co", "COURT_ORDER", "विशेष अदालत मुद्दा नं. ०८१-CR-०१३८ आदेश")
    _canonical("court_order", "special.081-cr-0138")
    report, _ = _run(tmp_path)
    assert report[m.iri]["outcome"] == "duplicate"
    assert report[m.iri]["canonical_iri"] == f"{BASE}/material/court_order/special.081-cr-0138"


def test_press_release_without_canonical_is_key_but_absent(tmp_path):
    m = _jawaf("20260507.pr2", "CIAA_PRESS_RELEASE", "CIAA प्रेस विज्ञप्ति नं. ९९९९")
    report, _ = _run(tmp_path)
    assert report[m.iri]["outcome"] == "key_but_absent"
    assert report[m.iri]["canonical_iri"] is None


def test_news_has_no_twin(tmp_path):
    m = _jawaf("20260507.news", "NEWS", "कुनै समाचार शीर्षक")
    report, _ = _run(tmp_path)
    assert report[m.iri]["outcome"] == "no_canonical_twin"


def test_charge_sheet_has_no_key(tmp_path):
    m = _jawaf("20260507.cs", "AG_ABHIYOG_PATRA", "आरोपपत्र — ०८१-CR-०१३८")
    report, _ = _run(tmp_path)
    assert report[m.iri]["outcome"] == "no_canonical_key"


def test_referencing_cases_are_reported(tmp_path):
    m = _jawaf("20260507.pr3", "CIAA_PRESS_RELEASE", "प्रेस विज्ञप्ति नं. ३१५५")
    _canonical("ciaa_press_release", "3155")
    case = Case.objects.create(
        case_type=CaseType.CORRUPTION, state=CaseState.PUBLISHED,
        title="Jhalak", slug="case-081-cr-0138-jhalak",
    )
    CaseMaterialReference.objects.create(case=case, material_iri=m.iri, ordinal=0)
    report, _ = _run(tmp_path)
    assert report[m.iri]["referencing_cases"] == ["case-081-cr-0138-jhalak"]


def test_command_mutates_nothing(tmp_path):
    m = _jawaf("20260507.pr4", "CIAA_PRESS_RELEASE", "प्रेस विज्ञप्ति नं. ३१५५")
    _canonical("ciaa_press_release", "3155")
    before_count = Material.objects.count()
    before_updated = Material.objects.get(pk=m.iri).updated_at
    _run(tmp_path)
    assert Material.objects.count() == before_count
    assert Material.objects.get(pk=m.iri).updated_at == before_updated
    assert Material.objects.filter(is_deleted=True).count() == 0
