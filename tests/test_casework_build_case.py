"""Unit tests for the unified agentic case-builder (casework/build_case.py).

The LLM call is mocked; we assert the payload normalization and the per-field
PATCH behavior (dry-run vs apply, skip-if-populated).
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from casework import build_case
from llm.invoke import salvage_json


class TestNormalizePayload(unittest.TestCase):
    def test_keeps_supported_fields_and_drops_bad_ones(self):
        payload = {
            "description": "## सारांश",
            "title": "नयाँ शीर्षक",
            "key_allegations": ["आरोप १", "  ", "आरोप २"],
            "timeline": [
                {"date": "2024-02-02", "title": "verdict", "date_bs": "2080-10-19"},
                {"date": "not-a-date", "title": "bad"},  # dropped
                {"date": "2024-01-01", "title": "complaint"},
            ],
            "bigo": 38671764,
            "tags": ["embezzlement", ""],
        }
        out = build_case._normalize_payload(payload)
        self.assertEqual(out["description"], "## सारांश")
        self.assertEqual(out["title"], "नयाँ शीर्षक")
        self.assertEqual(out["key_allegations"], ["आरोप १", "आरोप २"])
        self.assertEqual(
            [e["date"] for e in out["timeline"]], ["2024-01-01", "2024-02-02"]
        )
        self.assertEqual(out["bigo"], 38671764)
        self.assertEqual(out["tags"], ["embezzlement"])

    def test_rejects_bad_bigo(self):
        self.assertNotIn("bigo", build_case._normalize_payload({"bigo": True}))
        self.assertNotIn("bigo", build_case._normalize_payload({"bigo": 0}))
        self.assertNotIn("bigo", build_case._normalize_payload({"bigo": "5"}))

    def test_non_dict_payload(self):
        self.assertEqual(build_case._normalize_payload(["x"]), {})


class TestBuildContent(unittest.TestCase):
    def test_has_marker_and_task(self):
        content = build_case._build_content(
            {"title": "t", "court_cases": ["special:081-CR-0001"], "evidence": []},
            "SOME SOURCE TEXT",
            "## NGM court record",
        )
        self.assertIn(build_case._SOURCES_MARKER, content)
        self.assertIn("SOME SOURCE TEXT", content)
        self.assertIn("## NGM court record", content)
        self.assertIn("BUILD THE CASE RECORD", content)


_PAYLOAD = (
    '{"description": "## d", "title": "new title", '
    '"key_allegations": ["a1"], '
    '"timeline": [{"date": "2024-01-02", "date_bs": "2080-09-18", "title": "m"}], '
    '"bigo": 1000, "tags": ["t1"], '
    '"entities": [{"name": "X", "role": "accused"}]}'
)


def _case():
    # Evidence carries a >200-char description so source_content needs no network.
    return {
        "slug": "case-x",
        "title": "existing title",  # already populated -> title PATCH skipped
        "evidence": [
            {
                "description": "ने " * 120,
                "source": {"title": "PR", "source_type": "CIAA_PRESS_RELEASE"},
            }
        ],
    }


def _run(dry_run, force=False):
    api = MagicMock()
    stats = {
        k: 0
        for k in (
            "cases_processed",
            "cases_built",
            "cases_skipped",
            "cases_no_content",
            "cases_llm_error",
            "fields_patched",
            "fields_would_patch",
            "fields_already_populated",
        )
    }
    build_case._process_case(
        case=_case(),
        idx=1,
        total=1,
        args=SimpleNamespace(force=force, dry_run=dry_run, verbose=False),
        api=api,
        usage=MagicMock(),
        invoke_with_tools=lambda **kw: _PAYLOAD,
        salvage_json=salvage_json,
        case_markdown=lambda case: "",
        stats=stats,
    )
    return api, stats


class TestProcessCase(unittest.TestCase):
    def test_apply_patches_each_unpopulated_field(self):
        api, stats = _run(dry_run=False)
        patched = {c.args[1] for c in api.patch_field.call_args_list}
        # title skipped (already populated); the rest patched.
        self.assertEqual(
            patched, {"description", "key_allegations", "timeline", "bigo", "tags"}
        )
        self.assertEqual(stats["fields_patched"], 5)
        self.assertEqual(stats["fields_already_populated"], 1)  # title
        self.assertEqual(stats["cases_built"], 1)

    def test_dry_run_patches_nothing(self):
        api, stats = _run(dry_run=True)
        api.patch_field.assert_not_called()
        self.assertEqual(stats["fields_would_patch"], 5)

    def test_force_overwrites_title_too(self):
        api, _ = _run(dry_run=False, force=True)
        patched = {c.args[1] for c in api.patch_field.call_args_list}
        self.assertIn("title", patched)


if __name__ == "__main__":
    unittest.main()
