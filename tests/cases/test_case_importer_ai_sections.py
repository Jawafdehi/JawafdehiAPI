"""``scrape_case`` imports label their Gemini-written sections as AI-generated."""

import json

import pytest

from cases.services.case_importer import CaseImporter


def _import(tmp_path, **fields):
    path = tmp_path / "case-result.json"
    path.write_text(json.dumps({"title": "आयात गरिएको मुद्दा", **fields}), encoding="utf-8")
    return CaseImporter().import_from_json(str(path))


@pytest.mark.django_db
def test_marks_each_non_empty_section(tmp_path):
    case = _import(
        tmp_path,
        description="एआईले लेखेको विवरण",
        key_allegations=["सरकारी रकम हिनामिना गरेको आरोप"],
    )
    assert case.ai_generated_sections == ["description", "key_allegations"]


@pytest.mark.django_db
def test_marks_nothing_when_sections_empty(tmp_path):
    assert _import(tmp_path).ai_generated_sections == []
