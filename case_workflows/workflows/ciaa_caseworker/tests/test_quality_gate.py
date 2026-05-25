import textwrap

import pytest

from case_workflows.workflows.ciaa_caseworker.quality_gate import (
    evaluate_draft,
    write_quality_gate_report,
)


def _draft(**overrides) -> str:
    base = {
        "Case Metadata": "- **Title:** प्रकरण शीर्षक",
        "Entities": "| सुरेन्द्र | कर्मचारी | |",
        "Description": "मुद्दाको विस्तृत विवरण यहाँ लेखिएको छ।",
        "Key Allegations": "- पहिलो आरोपको विवरण",
        "Timeline": "### मुद्दा दायर — २०८१-०१-१५ (2024-04-28)",
        "Evidence / Sources": "### 1. आरोपपत्र",
    }
    base.update(overrides)
    return "\n\n".join(f"## {k}\n\n{v}" for k, v in base.items() if v)


def test_auto_publish():
    result = evaluate_draft(_draft())
    assert result["route"] == "auto_publish"
    assert result["overall_confidence"] == "high"
    assert all(result["checks"].values())


def test_missing_critical():
    result = evaluate_draft(_draft(**{"Entities": "", "Key Allegations": ""}))
    assert result["route"] == "hard_block"
    assert not result["checks"]["missing_critical_sections"]


def test_empty_section():
    result = evaluate_draft(_draft(**{"Description": ""}))
    assert result["route"] == "hard_block"


def test_english_leakage():
    large_english = "This is a very long English sentence that should trigger leakage detection when it appears multiple times across different sections. We are writing about procurement and corruption in Nepal but using excessive English words."
    result = evaluate_draft(_draft(**{"Description": large_english, "Key Allegations": large_english}))
    assert result["route"] in ("soft_flag", "hard_block")


def test_nepali_script():
    result = evaluate_draft(_draft(**{"Description": "Only English text with no Devanagari script characters whatsoever in this section. " * 12}))
    assert any(f["check"] == "nepali_script_presence" for f in result["findings"])


def test_html_validity():
    result = evaluate_draft(_draft(**{"Description": "<p>Valid HTML <strong>bold</strong> with unclosed <em>tag</p>"}))
    assert any(f["check"] == "html_validity" for f in result["findings"])


def test_hallucination_with_sources():
    result = evaluate_draft(
        _draft(
            **{
                "Description": "यो मुद्दा रु. १५,८८,५०,००० बिगोसँग सम्बन्धित छ।",
                "Timeline": "### 2024-04-28 — मुद्दा दायर",
            }
        ),
        {"charge-sheet.md": "रु. १५,८८,५०,००० 2024-04-28"},
    )
    assert result["route"] == "auto_publish"


def test_hallucination_unsupported_date():
    result = evaluate_draft(
        _draft(**{"Timeline": "### 2025-01-01 — काल्पनिक घटना"}),
        {"charge-sheet.md": "No matching date here."},
    )
    assert any(f["check"] == "hallucination_detection" for f in result["findings"])


def test_content_overlap():
    shared = "प्रतिवादीले सार्वजनिक खरिद ऐनको दफा २३ विपरीत कार्य गरी राज्यलाई हानि पुर्याएको देखिन्छ। यस प्रकरणमा संलग्न अन्य व्यक्तिहरूको पनि पहिचान गर्न आवश्यक छ।"
    result = evaluate_draft(_draft(**{"Description": shared, "Key Allegations": shared}))
    assert any(f["check"] == "content_overlap_detection" for f in result["findings"])


def test_quality_gate_tool_hard_block_report(tmp_path):
    case_dir = tmp_path / "case-abc"
    case_dir.mkdir()
    (case_dir / "draft.md").write_text(_draft(**{"Description": "", "Key Allegations": ""}))
    (case_dir / "sources" / "markdown").mkdir(parents=True)
    result = write_quality_gate_report(str(case_dir))
    assert "route=hard_block" in result
    report = case_dir / "quality-gate.json"
    assert report.exists()
    data = __import__("json").loads(report.read_text())
    assert data["route"] == "hard_block"


def test_passes_meaningful_section(tmp_path):
    case_dir = tmp_path / "case-def"
    case_dir.mkdir()
    (case_dir / "draft.md").write_text(_draft())
    (case_dir / "sources" / "markdown").mkdir(parents=True)
    result = write_quality_gate_report(str(case_dir))
    assert "route=auto_publish" in result
