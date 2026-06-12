"""
Tests for the API-driven enrich_ciaa_timeline management command.

The command reads cases, source content and NGM hearing records over the
Jawafdehi HTTP API and writes the timeline via PATCH — it never touches the
ORM. These tests therefore mock the HTTP layer (the command's _api_get /
_get_source_content / _get_ngm_data and the urllib PATCH) rather than creating
database rows. See https://github.com/Jawafdehi/JawafdehiAPI/issues/186.
"""

import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from cases.management.commands.enrich_ciaa_timeline import Command

API_BASE = "https://portal.jawafdehi.org/api"
COMMON = dict(
    api_base_url=API_BASE,
    api_token="tok",
    llm_api_key="llmkey",
)


def _case(**overrides):
    base = {
        "case_id": "case-0001",
        "slug": "case-0001-slug",
        "state": "DRAFT",
        "title": "Test CIAA case",
        "court_cases": ["special:081-CR-0060"],
        "timeline": [],
        "evidence": [],
    }
    base.update(overrides)
    return base


# ── case selection / filtering ───────────────────────────────────────────────


class TestCaseSelection:
    def _run_select(self, pages, **kwargs):
        cmd = Command()
        with patch.object(cmd, "_api_get", side_effect=pages):
            return cmd._get_ciaa_cases(
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
                **kwargs,
            )

    def test_selects_draft_special_court_cases(self):
        page = {"results": [_case()], "next": None}
        selected = self._run_select([page])
        assert [c["case_id"] for c in selected] == ["case-0001"]

    def test_skips_non_draft(self):
        page = {"results": [_case(state="PUBLISHED")], "next": None}
        assert self._run_select([page]) == []

    def test_skips_non_special_court(self):
        page = {"results": [_case(court_cases=["supreme:081-CR-0060"])], "next": None}
        assert self._run_select([page]) == []

    def test_skips_already_populated_unless_force(self):
        page = {
            "results": [_case(timeline=[{"date": "2025-01-01", "title": "x"}])],
            "next": None,
        }
        assert self._run_select([page]) == []

    def test_force_includes_populated(self):
        page = {
            "results": [_case(timeline=[{"date": "2025-01-01", "title": "x"}])],
            "next": None,
        }
        selected = self._run_select([page], force=True)
        assert len(selected) == 1

    def test_fiscal_year_filter(self):
        page = {
            "results": [
                _case(case_id="a", court_cases=["special:081-CR-0060"]),
                _case(case_id="b", court_cases=["special:080-CR-0010"]),
            ],
            "next": None,
        }
        selected = self._run_select([page], fiscal_year="081")
        assert [c["case_id"] for c in selected] == ["a"]

    def test_case_id_filter(self):
        page = {
            "results": [_case(case_id="a"), _case(case_id="b")],
            "next": None,
        }
        selected = self._run_select([page], case_id="b")
        assert [c["case_id"] for c in selected] == ["b"]

    def test_limit(self):
        page = {
            "results": [_case(case_id=f"c{i}") for i in range(5)],
            "next": None,
        }
        selected = self._run_select([page], limit=2)
        assert len(selected) == 2

    def test_follows_pagination(self):
        page1 = {"results": [_case(case_id="a")], "next": f"{API_BASE}/cases/?page=2"}
        page2 = {"results": [_case(case_id="b")], "next": None}
        selected = self._run_select([page1, page2])
        assert [c["case_id"] for c in selected] == ["a", "b"]


# ── response parsing ───────────────────────────────────────────────────────


class TestParseTimelineResponse:
    def setup_method(self):
        self.cmd = Command()

    def test_parses_clean_array_with_bs_and_span(self):
        text = json.dumps(
            [
                {
                    "date": "1989-07-14",
                    "date_bs": "2046-03-30",
                    "end_date": "2020-07-15",
                    "end_date_bs": "2077-03-31",
                    "title": "जाँच अवधि",
                    "description": "Investigation span.",
                },
                {
                    "date": "2025-02-09",
                    "date_bs": "2081-10-27",
                    "title": "मुद्दा दर्ता",
                },
            ]
        )
        entries = self.cmd._parse_timeline_response(text)
        assert len(entries) == 2
        # sorted chronologically
        assert entries[0]["date"] == "1989-07-14"
        assert entries[0]["date_bs"] == "2046-03-30"
        assert entries[0]["end_date"] == "2020-07-15"
        assert entries[0]["end_date_bs"] == "2077-03-31"
        assert entries[1]["date_bs"] == "2081-10-27"

    def test_strips_markdown_fences(self):
        text = '```json\n[{"date": "2025-02-09", "title": "x"}]\n```'
        entries = self.cmd._parse_timeline_response(text)
        assert entries and entries[0]["date"] == "2025-02-09"

    def test_invalid_json_returns_none(self):
        assert self.cmd._parse_timeline_response("not json") is None

    def test_empty_array_returns_none(self):
        assert self.cmd._parse_timeline_response("[]") is None

    def test_drops_entries_missing_required_fields(self):
        text = json.dumps(
            [
                {"date": "2025-02-09"},  # no title
                {"title": "no date"},  # no date
                {"date": "2025-02-09", "title": "ok"},
            ]
        )
        entries = self.cmd._parse_timeline_response(text)
        assert len(entries) == 1
        assert entries[0]["title"] == "ok"

    def test_drops_non_iso_date(self):
        text = json.dumps([{"date": "2081-10-27 BS", "title": "x"}])
        assert self.cmd._parse_timeline_response(text) is None

    def test_drops_end_date_before_start_but_keeps_entry(self):
        text = json.dumps(
            [{"date": "2020-07-15", "end_date": "1989-07-14", "title": "x"}]
        )
        entries = self.cmd._parse_timeline_response(text)
        assert len(entries) == 1
        assert "end_date" not in entries[0]

    def test_nested_timeline_key(self):
        text = json.dumps({"timeline": [{"date": "2025-02-09", "title": "x"}]})
        entries = self.cmd._parse_timeline_response(text)
        assert entries and entries[0]["title"] == "x"


# ── NGM section formatting ─────────────────────────────────────────────────


class TestFormatNgmSection:
    def test_flat_api_payload(self):
        cmd = Command()
        section = cmd._format_ngm_section(
            {
                "registration_date_ad": "2025-02-09",
                "case_status": "फैसला भएको",
                "verdict_date_ad": "2025-06-20",
                "verdict_judge": "Judge X",
                "hearings": [
                    {"hearing_date_ad": "2025-03-01", "decision_type": "पेशी"},
                ],
            }
        )
        assert "Case registration: 2025-02-09" in section
        assert "Hearings (1 records)" in section
        assert "Verdict date: 2025-06-20" in section
        assert "Judge X" in section

    def test_empty(self):
        assert Command()._format_ngm_section(None) == ""


# ── source content acquisition ─────────────────────────────────────────────


class TestSourceContent:
    def test_uses_long_description(self):
        cmd = Command()
        case = {
            "evidence": [
                {
                    "description": "x" * 250,
                    "source": {"source_type": "CIAA_PRESS_RELEASE", "urls": []},
                }
            ]
        }
        text = cmd._get_source_content(case)
        assert text and len(text) >= 250

    def test_uses_existing_markdown_link_when_description_short(self):
        cmd = Command()
        case = {
            "evidence": [
                {
                    "description": "short",
                    "source": {
                        "source_type": "AG_ABHIYOG_PATRA",
                        "urls": [
                            {
                                "role": "MARKDOWN",
                                "link": "https://s3.jawafdehi.org/x.md",
                            }
                        ],
                    },
                }
            ]
        }
        with patch.object(Command, "_download_text", return_value="m" * 300) as mock_dl:
            text = cmd._get_source_content(case)
        assert text and len(text) >= 300
        mock_dl.assert_called_once_with("https://s3.jawafdehi.org/x.md")

    def test_creates_markdown_via_shared_converter_when_none_exists(self):
        cmd = Command()
        case = {
            "evidence": [
                {
                    "description": "short",
                    "source": {
                        "source_type": "AG_ABHIYOG_PATRA",
                        "urls": [
                            {"role": "RAW", "link": "https://s3.jawafdehi.org/x.pdf"}
                        ],
                    },
                }
            ]
        }
        with patch(
            "review.converter.convert_source",
            return_value={"status": "converted", "markdown": "m" * 300, "note": ""},
        ) as mock_conv:
            text = cmd._get_source_content(case)
        assert text and len(text) >= 300
        # The shared converter was handed the convertible (RAW) link.
        _, kwargs = mock_conv.call_args
        passed = mock_conv.call_args.args[0]
        assert passed == {"url": ["https://s3.jawafdehi.org/x.pdf"]}

    def test_no_evidence_returns_none(self):
        assert Command()._get_source_content({"evidence": []}) is None

    def test_orders_by_milestone_source_type(self):
        cmd = Command()
        case = {
            "evidence": [
                {
                    "description": "B" * 250,
                    "source": {"source_type": "COURT_ORDER", "urls": []},
                },
                {
                    "description": "A" * 250,
                    "source": {"source_type": "AG_ABHIYOG_PATRA", "urls": []},
                },
            ]
        }
        text = cmd._get_source_content(case)
        # AG_ABHIYOG_PATRA is higher priority, so its content comes first.
        assert text.index("A" * 250) < text.index("B" * 250)


# ── end-to-end process_case (mocked HTTP + LLM) ─────────────────────────────


class TestProcessCase:
    def _cmd(self):
        cmd = Command()
        cmd.stdout = MagicMock()
        return cmd

    def test_dry_run_does_not_patch(self):
        cmd = self._cmd()
        case = _case()
        entries = [{"date": "2025-02-09", "date_bs": "2081-10-27", "title": "x"}]
        with patch.object(cmd, "_fetch_case_detail", return_value=case), patch.object(
            cmd, "_get_source_content", return_value="src"
        ), patch.object(cmd, "_get_ngm_data", return_value=None), patch.object(
            cmd, "_extract_timeline", return_value=entries
        ), patch.object(
            cmd, "_patch_timeline"
        ) as mock_patch:
            cmd._process_case(
                case=case,
                idx=1,
                total=1,
                dry_run=True,
                llm_cfg={
                    "backend": "openai",
                    "model": "m",
                    "base_url": "http://x/v1",
                    "api_key": "k",
                    "max_tokens": 4000,
                },
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
            )
        mock_patch.assert_not_called()
        assert cmd.stats["cases_enriched"] == 0

    def test_production_patches_timeline(self):
        cmd = self._cmd()
        case = _case()
        entries = [{"date": "2025-02-09", "date_bs": "2081-10-27", "title": "x"}]
        with patch.object(cmd, "_fetch_case_detail", return_value=case), patch.object(
            cmd, "_get_source_content", return_value="src"
        ), patch.object(cmd, "_get_ngm_data", return_value=None), patch.object(
            cmd, "_extract_timeline", return_value=entries
        ), patch.object(
            cmd, "_patch_timeline"
        ) as mock_patch:
            cmd._process_case(
                case=case,
                idx=1,
                total=1,
                dry_run=False,
                llm_cfg={
                    "backend": "openai",
                    "model": "m",
                    "base_url": "http://x/v1",
                    "api_key": "k",
                    "max_tokens": 4000,
                },
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
            )
        mock_patch.assert_called_once()
        _, kwargs = mock_patch.call_args
        assert kwargs["entries"] == entries
        assert kwargs["case_slug"] == "case-0001-slug"
        assert cmd.stats["cases_enriched"] == 1

    def test_no_content_skips(self):
        cmd = self._cmd()
        case = _case()
        with patch.object(cmd, "_fetch_case_detail", return_value=case), patch.object(
            cmd, "_get_source_content", return_value=None
        ), patch.object(cmd, "_get_ngm_data", return_value=None), patch.object(
            cmd, "_patch_timeline"
        ) as mock_patch:
            cmd._process_case(
                case=case,
                idx=1,
                total=1,
                dry_run=False,
                llm_cfg={
                    "backend": "openai",
                    "model": "m",
                    "base_url": "http://x/v1",
                    "api_key": "k",
                    "max_tokens": 4000,
                },
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
            )
        mock_patch.assert_not_called()
        assert cmd.stats["cases_no_content"] == 1


# ── PATCH payload ───────────────────────────────────────────────────────────


class TestPatchTimeline:
    def test_sends_json_patch_replace(self):
        cmd = Command()
        entries = [{"date": "2025-02-09", "date_bs": "2081-10-27", "title": "x"}]
        captured = {}

        class _Resp:
            def raise_for_status(self):
                return None

        def fake_patch(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["body"] = json
            captured["auth"] = (headers or {}).get("Authorization")
            return _Resp()

        session = MagicMock()
        session.patch.side_effect = fake_patch
        cmd._patch_timeline(
            case_slug="case-0001-slug",
            case_id="case-0001",
            entries=entries,
            api_base_url=API_BASE,
            api_token="tok",
            session=session,
        )
        assert captured["url"] == f"{API_BASE}/cases/case-0001-slug/"
        assert captured["auth"] == "Token tok"
        assert captured["body"] == [
            {"op": "replace", "path": "/timeline", "value": entries}
        ]

    def test_patch_http_error_raises_command_error(self):
        import requests

        cmd = Command()
        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 422
        resp.text = "bad timeline"
        err = requests.HTTPError(response=resp)
        resp.raise_for_status.side_effect = err
        session.patch.return_value = resp
        with pytest.raises(CommandError, match="422"):
            cmd._patch_timeline(
                case_slug="case-0001-slug",
                case_id="case-0001",
                entries=[{"date": "2025-02-09", "title": "x"}],
                api_base_url=API_BASE,
                api_token="tok",
                session=session,
            )

    def test_missing_slug_raises(self):
        with pytest.raises(CommandError, match="no slug"):
            Command()._patch_timeline(
                case_slug=None,
                case_id="case-0001",
                entries=[],
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
            )


# ── CLI argument handling ───────────────────────────────────────────────────


class TestCliArguments:
    def _run(self, **kwargs):
        out = StringIO()
        call_command("enrich_ciaa_timeline", stdout=out, **kwargs)
        return out.getvalue()

    def test_requires_api_token(self):
        with pytest.raises(CommandError, match="API token is required"):
            self._run(dry_run=True, llm_api_key="k")

    def test_openai_backend_requires_llm_key(self):
        with pytest.raises(CommandError, match="LLM API key"):
            self._run(dry_run=True, api_token="tok", llm_backend="openai")

    def test_bedrock_backend_does_not_require_llm_key(self):
        # Default model is Opus (bedrock) — no LLM key needed; runs with 0 cases.
        with patch.object(Command, "_get_ciaa_cases", return_value=[]):
            output = self._run(dry_run=True, api_token="tok", llm_backend="bedrock")
        assert "Backend: bedrock" in output
        assert "Timeline extraction complete" in output

    def test_priority_and_case_id_mutually_exclusive(self):
        with pytest.raises(CommandError, match="mutually exclusive"):
            self._run(priority=True, case_id="x", **COMMON)

    def test_invalid_fiscal_year(self):
        with pytest.raises(CommandError, match="Invalid fiscal year"):
            self._run(fiscal_year="20810", **COMMON)

    def test_invalid_limit(self):
        with pytest.raises(CommandError, match="Must be a positive integer"):
            self._run(limit=-1, **COMMON)

    def test_dry_run_runs_with_no_cases(self):
        # No DB access at all: mock the case fetch to return an empty list.
        with patch.object(Command, "_get_ciaa_cases", return_value=[]):
            output = self._run(dry_run=True, **COMMON)
        assert "Timeline extraction complete" in output

    def test_backend_auto_detects_bedrock_for_claude_model(self):
        cmd = Command()
        cfg = cmd._resolve_llm_config(
            {
                "llm_backend": "auto",
                "llm_model": "global.anthropic.claude-opus-4-8",
                "llm_max_tokens": 4000,
                "aws_profile": "",
                "aws_region": "us-west-2",
            }
        )
        assert cfg["backend"] == "bedrock"
        assert cfg["model"] == "global.anthropic.claude-opus-4-8"

    def test_backend_auto_detects_openai_for_other_model(self):
        cmd = Command()
        cfg = cmd._resolve_llm_config(
            {
                "llm_backend": "auto",
                "llm_model": "qwen.qwen3-235b-a22b-2507",
                "llm_max_tokens": 4000,
                "llm_base_url": "http://x/v1",
                "llm_api_key": "k",
            }
        )
        assert cfg["backend"] == "openai"
        assert cfg["api_key"] == "k"
