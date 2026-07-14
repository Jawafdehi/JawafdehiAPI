"""Tests for the ``dedup_jawafdehi_materials`` management command.

Focus on the DETECT (``--dry-run``, the default) surface: it scans
``/material/jawafdehi/*`` materials, classifies each via ``materials.dedup``, writes a
JSONL report + a summary, and MUTATES NOTHING. The mutating ``--apply`` path's semantics
are covered by ``test_dedup_merge``; here we only assert the command wires ``--apply``,
``--dry-run`` guarding, the merge plan, and ``--output -`` streaming. Runs on sqlite.
"""

from __future__ import annotations

import io
import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

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


def _run(tmp_path, *args):
    out = tmp_path / "report.jsonl"
    stdout = io.StringIO()
    call_command("dedup_jawafdehi_materials", "--output", str(out), *args, stdout=stdout)
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


def test_referencing_cases_and_plan_are_reported(tmp_path):
    m = _jawaf("20260507.pr3", "CIAA_PRESS_RELEASE", "प्रेस विज्ञप्ति नं. ३१५५")
    _canonical("ciaa_press_release", "3155")
    case = Case.objects.create(
        case_type=CaseType.CORRUPTION, state=CaseState.PUBLISHED,
        title="Jhalak", slug="case-081-cr-0138-jhalak",
    )
    CaseMaterialReference.objects.create(case=case, material_iri=m.iri, ordinal=0)
    report, _ = _run(tmp_path)
    row = report[m.iri]
    assert row["referencing_cases"] == ["case-081-cr-0138-jhalak"]
    # Dry-run duplicates carry a merge plan (mutating nothing).
    assert row["plan"]["refs_to_repoint"] == ["case-081-cr-0138-jhalak"]
    assert row["plan"]["collisions"] == []


def test_default_run_mutates_nothing(tmp_path):
    m = _jawaf("20260507.pr4", "CIAA_PRESS_RELEASE", "प्रेस विज्ञप्ति नं. ३१५५")
    _canonical("ciaa_press_release", "3155")
    case = Case.objects.create(
        case_type=CaseType.CORRUPTION, state=CaseState.PUBLISHED,
        title="X", slug="case-x",
    )
    CaseMaterialReference.objects.create(case=case, material_iri=m.iri, ordinal=0)
    before_count = Material.objects.count()
    before_updated = Material.objects.get(pk=m.iri).updated_at
    _run(tmp_path)
    assert Material.objects.count() == before_count
    assert Material.objects.get(pk=m.iri).updated_at == before_updated
    assert Material.objects.filter(is_deleted=True).count() == 0
    # The reference was not repointed.
    assert CaseMaterialReference.objects.get(case=case).material_iri == m.iri


def test_apply_and_dry_run_are_mutually_exclusive(tmp_path):
    with pytest.raises(CommandError):
        call_command(
            "dedup_jawafdehi_materials", "--apply", "--dry-run",
            "--output", str(tmp_path / "r.jsonl"),
        )


def test_apply_repoints_and_soft_deletes(tmp_path):
    m = _jawaf("20260507.pr5", "CIAA_PRESS_RELEASE", "प्रेस विज्ञप्ति नं. ३१५५")
    canonical = _canonical("ciaa_press_release", "3155")
    case = Case.objects.create(
        case_type=CaseType.CORRUPTION, state=CaseState.PUBLISHED,
        title="Y", slug="case-y",
    )
    CaseMaterialReference.objects.create(case=case, material_iri=m.iri, ordinal=0)

    report, summary = _run(tmp_path, "--apply")

    assert report[m.iri]["applied"] == {
        "refs_repointed": 1, "refs_deduped": 0, "soft_deleted": True,
    }
    assert Material.objects.get(pk=m.iri).is_deleted is True
    assert CaseMaterialReference.objects.get(case=case).material_iri == canonical.iri
    assert "Merged 1 of" in summary


def test_court_order_matches_canonical_with_uppercase_ident(tmp_path):
    # The jawafdehi case number is lowercased; a stored court_order ident may carry
    # an uppercase code. iendswith must still match it.
    m = _jawaf("20260507.co2", "COURT_ORDER", "मुद्दा नं. ०८१-CR-०१३८ आदेश")
    _canonical("court_order", "special.081-CR-0138")
    report, _ = _run(tmp_path)
    assert report[m.iri]["outcome"] == "duplicate"
    assert report[m.iri]["canonical_iri"].endswith("/court_order/special.081-CR-0138")


def test_limit_caps_rows_and_zero_processes_nothing(tmp_path):
    _jawaf("20260507.a", "NEWS", "a")
    _jawaf("20260507.b", "NEWS", "b")
    report_one, _ = _run(tmp_path, "--limit", "1")
    assert len(report_one) == 1
    report_zero, summary = _run(tmp_path, "--limit", "0")
    assert len(report_zero) == 0
    assert "0 of 0 jawafdehi materials" in summary


def test_output_dash_streams_jsonl_to_stdout_summary_to_stderr():
    _jawaf("20260507.news2", "NEWS", "समाचार")
    stdout, stderr = io.StringIO(), io.StringIO()
    call_command("dedup_jawafdehi_materials", "--output", "-", stdout=stdout, stderr=stderr)
    # stdout is pure JSONL (every non-empty line parses); summary is on stderr.
    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    assert lines and all(json.loads(ln) for ln in lines)
    assert "duplicate a document we already hold" in stderr.getvalue()
    assert "duplicate a document we already hold" not in stdout.getvalue()
