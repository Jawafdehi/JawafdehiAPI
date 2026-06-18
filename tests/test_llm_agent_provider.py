"""Unit tests for the agentic ClaudeAgentProvider run-wild -> judge -> iterate loop.

The claude subprocess (_run) and the completeness-judge provider are both mocked,
so no real CLI / MCP is invoked.
"""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import override_settings

from llm.providers.agent import ClaudeAgentProvider
from llm.usage import UsageAccumulator


def _envelope(result_str, cost=0.0):
    """A claude -p --output-format json envelope wrapping `result_str`."""
    return json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": result_str,
            "total_cost_usd": cost,
            "usage": {
                "input_tokens": 5,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 7,
            },
            "modelUsage": {"claude-opus-4-8": {}},
        }
    )


def _fake_judge(*verdicts):
    """A mock judge provider whose invoke_text yields the given verdict JSONs."""
    judge = MagicMock()
    judge.model_for_tier.return_value = "cheap-model"
    judge.invoke_text.side_effect = list(verdicts)
    return judge


class TestClaudeAgentProvider(unittest.TestCase):
    @override_settings(CLAUDE_AGENT_MAX_ITERS=3, CLAUDE_AGENT_JUDGE_PROVIDER="")
    def test_single_pass_structural_only(self):
        """No judge provider -> structural valid-JSON check only -> one run."""
        p = ClaudeAgentProvider()
        with patch.object(
            p, "_run", return_value=_envelope('{"rules": {}}', cost=0.01)
        ) as run:
            usage = UsageAccumulator()
            out = p.invoke_text("sys", "task", 900, "claude-opus-4-8", "premium", usage)
        self.assertEqual(out, '{"rules": {}}')
        run.assert_called_once()
        bucket = usage.as_dict()["by_provider"][0]
        self.assertEqual(bucket["provider"], "claude_agent")
        self.assertEqual(bucket["tier"], "premium")
        self.assertEqual(usage.input_tokens, 5)
        self.assertEqual(usage.output_tokens, 7)

    @override_settings(CLAUDE_AGENT_MAX_ITERS=3, CLAUDE_AGENT_JUDGE_PROVIDER="proxy")
    def test_iterates_until_judge_satisfied(self):
        """First answer judged incomplete -> re-run -> second judged complete."""
        p = ClaudeAgentProvider()
        outputs = [
            _envelope('{"rules": {"a": 1}}'),
            _envelope('{"rules": {"a": 1, "b": 2}}'),
        ]
        judge = _fake_judge(
            '{"complete": false, "missing": ["rule b"]}',
            '{"complete": true, "missing": []}',
        )
        with patch.object(p, "_run", side_effect=outputs) as run, patch(
            "llm.routing.get_provider", return_value=judge
        ):
            out = p.invoke_text("sys", "task", 900, "m", "premium", UsageAccumulator())
        self.assertEqual(run.call_count, 2)
        self.assertEqual(out, '{"rules": {"a": 1, "b": 2}}')

    @override_settings(CLAUDE_AGENT_MAX_ITERS=2, CLAUDE_AGENT_JUDGE_PROVIDER="proxy")
    def test_iteration_cap_enforced(self):
        """Judge never satisfied -> stop at the cap and return the last answer."""
        p = ClaudeAgentProvider()
        judge = MagicMock()
        judge.model_for_tier.return_value = "c"
        judge.invoke_text.return_value = '{"complete": false, "missing": ["x"]}'
        with patch.object(
            p, "_run", return_value=_envelope('{"rules": {}}')
        ) as run, patch("llm.routing.get_provider", return_value=judge):
            out = p.invoke_text("sys", "task", 900, "m", "premium", UsageAccumulator())
        self.assertEqual(run.call_count, 2)
        self.assertEqual(out, '{"rules": {}}')

    @override_settings(CLAUDE_AGENT_MAX_ITERS=2, CLAUDE_AGENT_JUDGE_PROVIDER="")
    def test_invalid_json_triggers_rerun(self):
        """An unparseable answer is structurally incomplete -> re-run."""
        p = ClaudeAgentProvider()
        with patch.object(
            p,
            "_run",
            side_effect=[_envelope("not json at all"), _envelope('{"ok": 1}')],
        ) as run:
            out = p.invoke_text("sys", "task", 900, "m", "premium", UsageAccumulator())
        self.assertEqual(run.call_count, 2)
        self.assertEqual(out, '{"ok": 1}')

    @override_settings(
        CLAUDE_AGENT_MAX_ITERS=3, CLAUDE_AGENT_JUDGE_PROVIDER="claude_agent"
    )
    def test_self_judge_avoids_recursion(self):
        """Judge provider == self -> structural only (no recursive agent call)."""
        p = ClaudeAgentProvider()
        with patch.object(p, "_run", return_value=_envelope('{"ok": 1}')) as run:
            out = p.invoke_text("sys", "task", 900, "m", "premium", UsageAccumulator())
        run.assert_called_once()
        self.assertEqual(out, '{"ok": 1}')

    @override_settings(CLAUDE_AGENT_MAX_ITERS=2, CLAUDE_AGENT_JUDGE_PROVIDER="proxy")
    def test_judge_failure_accepts_answer(self):
        """A throwing judge must not burn iterations -> accept after one run."""
        p = ClaudeAgentProvider()
        judge = MagicMock()
        judge.model_for_tier.return_value = "c"
        judge.invoke_text.side_effect = RuntimeError("judge down")
        with patch.object(p, "_run", return_value=_envelope('{"ok": 1}')) as run, patch(
            "llm.routing.get_provider", return_value=judge
        ):
            out = p.invoke_text("sys", "task", 900, "m", "premium", UsageAccumulator())
        run.assert_called_once()
        self.assertEqual(out, '{"ok": 1}')

    def test_model_for_tier_returns_string(self):
        p = ClaudeAgentProvider()
        self.assertIsInstance(p.model_for_tier("premium"), str)
        self.assertIsInstance(p.model_for_tier("cheap"), str)

    @override_settings(
        CLAUDE_AGENT_BIN="claude",
        CLAUDE_AGENT_MCP_CONFIG="/tmp/mcp.json",
        CLAUDE_AGENT_ALLOWED_TOOLS="Read Glob Grep mcp__jawafdehi__convert_date",
        CLAUDE_AGENT_DISALLOWED_TOOLS="Bash Write Edit",
        CLAUDE_AGENT_MAX_TURNS=12,
    )
    def test_argv_reads_staged_files_sandboxed(self):
        """argv lets the agent Read the staging dir + convert_date; denies shell/web."""
        p = ClaudeAgentProvider()
        argv = p._build_argv("system prompt", "opus", "/tmp/review-staging")
        self.assertIn("-p", argv)
        self.assertIn("--add-dir", argv)
        self.assertIn("/tmp/review-staging", argv)
        self.assertIn("--mcp-config", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--allowedTools", argv)
        self.assertIn("Read", argv)
        self.assertIn("mcp__jawafdehi__convert_date", argv)
        self.assertIn("--disallowedTools", argv)
        self.assertIn("Bash", argv)
        self.assertIn("--max-turns", argv)
        self.assertIn("12", argv)
        # Agentic, not plan-locked; and NOT the old source-fetch tool.
        self.assertNotIn("plan", argv)
        self.assertNotIn("mcp__jawafdehi__convert_to_markdown", argv)


class TestAgentStaging(unittest.TestCase):
    """The provider stages judge content into markdown files for the agent to read."""

    def test_split_content_list(self):
        from llm.providers.agent import _split_content

        materials, instruction = _split_content(
            [{"type": "text", "text": "CONTEXT"}, {"type": "text", "text": "RULES"}]
        )
        self.assertEqual(materials, "CONTEXT")
        self.assertEqual(instruction, "RULES")

    def test_split_content_string(self):
        from llm.providers.agent import _split_content

        materials, instruction = _split_content("everything")
        self.assertEqual(materials, "everything")
        self.assertEqual(instruction, "")

    def test_stage_materials_splits_on_sources_marker(self):
        import shutil
        import tempfile

        from llm.providers.agent import _stage_materials

        d = tempfile.mkdtemp()
        try:
            mats = (
                "CASE DATA:\n{...}\n\nSOURCE DOCUMENT EXCERPTS (md):\n## [source 1]..."
            )
            files = _stage_materials(d, mats)
            self.assertEqual(files, ["case.md", "sources.md"])
            self.assertIn("CASE DATA", (Path(d) / "case.md").read_text())
            self.assertIn(
                "SOURCE DOCUMENT EXCERPTS", (Path(d) / "sources.md").read_text()
            )
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_stage_materials_fallback_single_file(self):
        import shutil
        import tempfile

        from llm.providers.agent import _stage_materials

        d = tempfile.mkdtemp()
        try:
            files = _stage_materials(d, "no marker here")
            self.assertEqual(files, ["materials.md"])
            self.assertTrue((Path(d) / "materials.md").exists())
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestAgentSampling(unittest.TestCase):
    """The agent grades the full rule batch CLAUDE_AGENT_SAMPLES times (mean+variance)."""

    @override_settings(
        REVIEW_LLM_PROVIDER_PREMIUM="claude_agent",
        REVIEW_LLM_PROVIDER_CHEAP="claude_agent",
        CLAUDE_AGENT_SAMPLES=2,
        REVIEW_CLI_MAX_WORKERS=1,
    )
    def test_agent_grades_each_rule_n_samples(self):
        from review import judge

        rules = [
            {"key": "r1", "title": "R1", "description": "d", "is_gate": False},
            {"key": "r2", "title": "R2", "description": "d", "is_gate": False},
        ]
        calls = []

        def fake_invoke_json(
            system, content, max_tokens=900, tier="premium", usage=None
        ):
            if isinstance(content, list):  # batch content (cache blocks)
                calls.append("batch")
                return {
                    "rules": {
                        "r1": {
                            "score": 80,
                            "rationale": "x",
                            "issues": [],
                            "suggestions": [],
                        },
                        "r2": {
                            "score": 60,
                            "rationale": "y",
                            "issues": [],
                            "suggestions": [],
                        },
                    }
                }
            calls.append("narrative")  # narrative prompt is a plain string
            return {"narrative": "n"}

        with patch("review.judge.invoke_json", side_effect=fake_invoke_json):
            out = judge.judge_rules(
                {"title": "t"}, "excerpts", "label", rules, n_samples=1
            )

        self.assertEqual(calls.count("batch"), 2)  # CLAUDE_AGENT_SAMPLES
        self.assertEqual(out["r1"]["samples"], [80, 80])
        self.assertEqual(out["r1"]["mean"], 80)
        self.assertEqual(out["r2"]["mean"], 60)


class TestNgmRender(unittest.TestCase):
    """The NGM court record renders to markdown for the judge / staged files."""

    def test_court_case_md_renders_fields(self):
        from review.ngm_render import court_case_md

        md = court_case_md(
            "special:081-CR-0079",
            {
                "court": "special",
                "case_number": "081-CR-0079",
                "status": "decided",
                "verdict_date_ad": "2024-01-02",
                "entities": [
                    {"side": "defendant", "name": "X"},
                    {"side": "plaintiff", "name": "CIAA"},
                ],
                "hearings": [{"date_ad": "2023-12-01", "type": "hearing"}],
            },
        )
        self.assertIn("special:081-CR-0079", md)
        self.assertIn("verdict_date_ad", md)
        self.assertIn("defendant", md)
        self.assertIn("Hearings", md)

    def test_court_case_md_missing_record(self):
        from review.ngm_render import court_case_md

        self.assertIn("no matching record", court_case_md("special:nope", None))


if __name__ == "__main__":
    unittest.main()
