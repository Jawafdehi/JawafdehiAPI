"""
Tests for the API-driven enrich_ciaa_description management command.

Like enrich_ciaa_timeline, this command reads cases + source content over the
Jawafdehi HTTP API and writes the description/title via PATCH — it never touches
the ORM. These tests mock the HTTP + LLM layers rather than creating DB rows.
See https://github.com/Jawafdehi/JawafdehiAPI/issues/199.
"""

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from cases.management.commands.enrich_ciaa_description import Command

API_BASE = "https://portal.jawafdehi.org/api"
COMMON = dict(api_base_url=API_BASE, api_token="tok", llm_api_key="llmkey")


def _case(**overrides):
    base = {
        "case_id": "case-0001",
        "slug": "case-0001-slug",
        "state": "DRAFT",
        "title": "Test CIAA case (080-CR-0047)",
        "court_cases": ["special:080-CR-0047"],
        "description": "",
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
                api_base_url=API_BASE, api_token="tok", session=MagicMock(), **kwargs
            )

    def test_selects_draft_special_court_cases(self):
        page = {"results": [_case()], "next": None}
        assert [c["case_id"] for c in self._run_select([page])] == ["case-0001"]

    def test_skips_non_draft(self):
        page = {"results": [_case(state="PUBLISHED")], "next": None}
        assert self._run_select([page]) == []

    def test_skips_non_special_court(self):
        page = {"results": [_case(court_cases=["supreme:081-CR-0060"])], "next": None}
        assert self._run_select([page]) == []

    def test_skips_already_described_unless_force(self):
        # A substantial (>=600 char) description is treated as already populated.
        page = {"results": [_case(description="x" * 700)], "next": None}
        assert self._run_select([page]) == []

    def test_thin_description_is_not_populated(self):
        page = {"results": [_case(description="too short")], "next": None}
        assert [c["case_id"] for c in self._run_select([page])] == ["case-0001"]

    def test_force_includes_described(self):
        page = {"results": [_case(description="x" * 700)], "next": None}
        assert len(self._run_select([page], force=True)) == 1

    def test_case_id_filter(self):
        page = {
            "results": [_case(), _case(case_id="case-0002")],
            "next": None,
        }
        out = self._run_select([page], case_id="case-0002")
        assert [c["case_id"] for c in out] == ["case-0002"]

    def test_limit(self):
        page = {
            "results": [_case(case_id=f"case-{i}") for i in range(5)],
            "next": None,
        }
        assert len(self._run_select([page], limit=2)) == 2

    def test_follows_pagination(self):
        p1 = {"results": [_case(case_id="a")], "next": f"{API_BASE}/cases/?page=2"}
        p2 = {"results": [_case(case_id="b")], "next": None}
        assert [c["case_id"] for c in self._run_select([p1, p2])] == ["a", "b"]

    def test_case_id_stops_paging_once_found(self):
        # The requested case is on page 1; the loop must NOT fetch page 2.
        p1 = {
            "results": [_case(case_id="want"), _case(case_id="other")],
            "next": f"{API_BASE}/cases/?page=2",
        }
        # If page 2 were fetched, side_effect would be exhausted (one page only).
        cmd = Command()
        with patch.object(cmd, "_api_get", side_effect=[p1]) as api_get:
            out = cmd._get_ciaa_cases(
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
                case_id="want",
            )
        assert [c["case_id"] for c in out] == ["want"]
        assert api_get.call_count == 1  # stopped after the match, no page 2

    def test_case_id_non_draft_stops_paging(self):
        # The requested case exists but isn't a DRAFT — return immediately.
        p1 = {
            "results": [_case(case_id="want", state="PUBLISHED")],
            "next": f"{API_BASE}/cases/?page=2",
        }
        cmd = Command()
        with patch.object(cmd, "_api_get", side_effect=[p1]) as api_get:
            out = cmd._get_ciaa_cases(
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
                case_id="want",
            )
        assert out == []
        assert api_get.call_count == 1

    def test_case_id_failing_filter_stops_paging(self):
        # The match fails a downstream filter (not special-court) — still must
        # return immediately rather than paging the rest of the list.
        p1 = {
            "results": [_case(case_id="want", court_cases=["supreme:081-CR-1"])],
            "next": f"{API_BASE}/cases/?page=2",
        }
        cmd = Command()
        with patch.object(cmd, "_api_get", side_effect=[p1]) as api_get:
            out = cmd._get_ciaa_cases(
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
                case_id="want",
            )
        assert out == []
        assert api_get.call_count == 1


# ── response parsing ─────────────────────────────────────────────────────────


class TestParseResponse:
    def _p(self, text):
        return Command._parse_response(text)

    def test_parses_clean_object(self):
        out = self._p('{"title": "शीर्षक (080-CR-0047)", "description": "### क) सार"}')
        assert out == {"title": "शीर्षक (080-CR-0047)", "description": "### क) सार"}

    def test_strips_markdown_fences(self):
        out = self._p('```json\n{"title": "t", "description": "d"}\n```')
        assert out == {"title": "t", "description": "d"}

    def test_ignores_prose_around_object(self):
        out = self._p('Here you go:\n{"title": "t", "description": "d"}\nDone.')
        assert out == {"title": "t", "description": "d"}

    def test_null_title_becomes_none(self):
        out = self._p('{"title": null, "description": "d"}')
        assert out == {"title": None, "description": "d"}

    def test_braces_inside_string_do_not_break_parsing(self):
        out = self._p('{"title": "t", "description": "uses {curly} braces"}')
        assert out == {"title": "t", "description": "uses {curly} braces"}

    def test_invalid_json_returns_none(self):
        assert self._p("not json at all") is None

    def test_no_object_returns_none(self):
        assert self._p("") is None

    def test_leading_prose_with_braces_then_object(self):
        # Prose that itself contains braces must not abort the parse — the
        # parser tries every '{' and returns the first dict with a description.
        out = self._p(
            "Here is the JSON {title, description}: "
            '{"title": "t", "description": "d"}'
        )
        assert out == {"title": "t", "description": "d"}

    def test_skips_brace_block_without_description(self):
        out = self._p('{"unrelated": 1} then {"title": "t", "description": "d"}')
        assert out == {"title": "t", "description": "d"}


# ── title validation (mirrors the court_number_in_title review gate) ──────────


class TestValidateTitle:
    def test_accepts_matching_number(self):
        assert Command._validate_title("शीर्षक (080-CR-0047)", "080-CR-0047") is None

    def test_rejects_missing_number(self):
        msg = Command._validate_title("शीर्षक without a number", "080-CR-0047")
        assert msg and "no court case number" in msg

    def test_rejects_mismatched_number(self):
        msg = Command._validate_title("शीर्षक (081-CR-9999)", "080-CR-0047")
        assert msg and "do not include" in msg

    def test_case_insensitive(self):
        assert Command._validate_title("t (080-cr-0047)", "080-CR-0047") is None

    def test_rejects_number_with_extra_trailing_digit(self):
        # COURT_RE is anchored: "080-CR-00478" must NOT match "080-CR-0047".
        msg = Command._validate_title("शीर्षक (080-CR-00478)", "080-CR-0047")
        assert msg is not None

    def test_rejects_number_with_extra_leading_digit(self):
        msg = Command._validate_title("शीर्षक (1080-CR-0047)", "080-CR-0047")
        assert msg is not None

    def test_rejects_number_not_at_end(self):
        # The number is present and matches, but not at the end → rejected.
        msg = Command._validate_title("(080-CR-0047) शीर्षक बीचमा नम्बर", "080-CR-0047")
        assert msg and "must end with" in msg

    def test_accepts_number_at_end_with_trailing_space(self):
        assert Command._validate_title("शीर्षक (080-CR-0047)  ", "080-CR-0047") is None


class TestFormatBigo:
    def test_positive_int_formatted_with_commas(self):
        assert Command._format_bigo(10403941) == "10,403,941"

    def test_none_is_unknown(self):
        assert Command._format_bigo(None) == "(unknown)"

    def test_zero_is_unknown(self):
        assert Command._format_bigo(0) == "(unknown)"

    def test_non_numeric_is_unknown(self):
        assert Command._format_bigo("abc") == "(unknown)"


class TestTitleHeadcount:
    def test_flags_jana_count(self):
        # Real bad output from case 080-CR-0070.
        assert Command._title_has_headcount(
            "…पदाधिकारीसमेत १२ जना सबैले सफाई (080-CR-0070)"
        )

    def test_flags_pratibadi_count(self):
        # Real bad output from case 080-CR-0098.
        assert Command._title_has_headcount(
            "…तीन तत्कालीन अध्यक्षसहित २४९ प्रतिवादीमाथि भ्रष्टाचार (080-CR-0098)"
        )

    def test_flags_ascii_count(self):
        assert Command._title_has_headcount("scheme with 12 जना (080-CR-0001)")

    def test_court_number_alone_is_not_a_headcount(self):
        assert not Command._title_has_headcount(
            "सचिव संजय शर्मासहित भ्रष्टाचार (080-CR-0098)"
        )

    def test_clean_title_passes(self):
        assert not Command._title_has_headcount(
            "नापी अधिकृत राजु पुरीले रु.१.०४ करोड आर्जन (080-CR-0007)"
        )


# ── source assembly + two-pass verdict summarisation ─────────────────────────


class TestAssembleSourceText:
    def _cmd(self):
        cmd = Command()
        cmd.stdout = StringIO()
        return cmd

    def test_short_sources_pass_through_whole(self):
        cmd = self._cmd()
        parts = [("CIAA_PRESS_RELEASE", "press"), ("COURT_ORDER", "short verdict")]
        out = cmd._assemble_source_text(parts, {"backend": "bedrock"}, MagicMock())
        assert "press" in out and "short verdict" in out

    def test_long_verdict_is_summarised(self):
        cmd = self._cmd()
        long_verdict = "व" * 20000  # over VERDICT_SUMMARY_TRIGGER
        parts = [("COURT_ORDER", long_verdict)]
        with patch.object(cmd, "_summarize_verdict", return_value="सारांश") as m:
            out = cmd._assemble_source_text(parts, {"backend": "bedrock"}, MagicMock())
        m.assert_called_once()
        assert "सारांश" in out
        assert "फैसला सारांश" in out
        assert long_verdict not in out

    def test_long_verdict_summary_failure_falls_back_to_head(self):
        cmd = self._cmd()
        long_verdict = "व" * 20000
        parts = [("COURT_ORDER", long_verdict)]
        with patch.object(cmd, "_summarize_verdict", return_value=None):
            out = cmd._assemble_source_text(parts, {"backend": "bedrock"}, MagicMock())
        # Falls back to a truncated head, not the full document.
        assert "व" in out
        assert len(out) < len(long_verdict)

    def test_budget_caps_total(self):
        cmd = self._cmd()
        from cases.management.commands import enrich_ciaa_description as mod

        parts = [("CIAA_PRESS_RELEASE", "a" * (mod.SOURCE_TEXT_BUDGET + 5000))]
        out = cmd._assemble_source_text(parts, {"backend": "bedrock"}, MagicMock())
        assert len(out) <= mod.SOURCE_TEXT_BUDGET + 50  # + label overhead


# ── NGM section formatting ────────────────────────────────────────────────────


class TestFormatNgmSection:
    def test_flat_payload(self):
        out = Command._format_ngm_section(
            {
                "registration_date_ad": "2023-08-10",
                "case_status": "decided",
                "verdict_date_ad": "2024-03-14",
                "verdict_judge": "टेक नारायण कुँवर",
            }
        )
        assert "2023-08-10" in out and "टेक नारायण कुँवर" in out

    def test_empty(self):
        assert Command._format_ngm_section(None) == ""


# ── source content acquisition ───────────────────────────────────────────────


class TestSourceParts:
    def test_no_evidence_returns_empty(self):
        assert Command()._get_source_parts({"evidence": []}) == []

    def test_orders_by_source_type_priority(self):
        cmd = Command()
        case = {
            "evidence": [
                {"source": {"source_type": "COURT_ORDER"}},
                {"source": {"source_type": "AG_ABHIYOG_PATRA"}},
                {"source": {"source_type": "CIAA_PRESS_RELEASE"}},
            ]
        }
        with patch.object(
            cmd, "_content_from_evidence_entry", side_effect=lambda e: "x" * 300
        ):
            parts = cmd._get_source_parts(case)
        # Charge sheet first, then press release, then court order.
        assert [p[0] for p in parts] == [
            "AG_ABHIYOG_PATRA",
            "CIAA_PRESS_RELEASE",
            "COURT_ORDER",
        ]


# ── process_case: dry-run vs patch ────────────────────────────────────────────


class TestProcessCase:
    def _cmd(self):
        cmd = Command()
        cmd.stdout = StringIO()
        return cmd

    def _result(self):
        return {"title": "नयाँ शीर्षक (080-CR-0047)", "description": "### क) सार"}

    def test_dry_run_does_not_patch(self):
        cmd = self._cmd()
        with patch.object(
            cmd, "_fetch_case_detail", return_value=_case()
        ), patch.object(
            cmd, "_get_source_parts", return_value=[("CIAA_PRESS_RELEASE", "x" * 300)]
        ), patch.object(
            cmd, "_get_ngm_data", return_value=None
        ), patch.object(
            cmd, "_generate", return_value=self._result()
        ), patch.object(
            cmd, "_patch_case"
        ) as patch_case:
            out, delta = cmd._process_case(
                case=_case(),
                idx=1,
                total=1,
                dry_run=True,
                force=False,
                skip_title=False,
                benchmark_dir=None,
                llm_cfg={"backend": "bedrock"},
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
            )
        patch_case.assert_not_called()
        # _process_case is thread-safe: it writes to the returned buffer, not stdout.
        assert "नयाँ शीर्षक" in out.getvalue()
        assert delta == {"cases_processed": 1}

    def test_patch_writes_description_and_title(self):
        cmd = self._cmd()
        with patch.object(
            cmd, "_fetch_case_detail", return_value=_case()
        ), patch.object(
            cmd, "_get_source_parts", return_value=[("CIAA_PRESS_RELEASE", "x" * 300)]
        ), patch.object(
            cmd, "_get_ngm_data", return_value=None
        ), patch.object(
            cmd, "_generate", return_value=self._result()
        ), patch.object(
            cmd, "_patch_case"
        ) as patch_case:
            cmd._process_case(
                case=_case(),
                idx=1,
                total=1,
                dry_run=False,
                force=False,
                skip_title=False,
                benchmark_dir=None,
                llm_cfg={"backend": "bedrock"},
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
            )
        patch_case.assert_called_once()
        kwargs = patch_case.call_args.kwargs
        assert kwargs["description"] == "### क) सार"
        assert kwargs["title"] == "नयाँ शीर्षक (080-CR-0047)"

    def test_patch_skips_bad_title(self):
        # A regenerated title that fails the court-number gate is not written.
        cmd = self._cmd()
        bad = {"title": "शीर्षक (999-XX-9999)", "description": "### क) सार"}
        with patch.object(
            cmd, "_fetch_case_detail", return_value=_case()
        ), patch.object(
            cmd, "_get_source_parts", return_value=[("CIAA_PRESS_RELEASE", "x" * 300)]
        ), patch.object(
            cmd, "_get_ngm_data", return_value=None
        ), patch.object(
            cmd, "_generate", return_value=bad
        ), patch.object(
            cmd, "_patch_case"
        ) as patch_case:
            cmd._process_case(
                case=_case(),
                idx=1,
                total=1,
                dry_run=False,
                force=False,
                skip_title=False,
                benchmark_dir=None,
                llm_cfg={"backend": "bedrock"},
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
            )
        # description still written, but title suppressed.
        assert patch_case.call_args.kwargs["title"] is None

    def test_no_content_skips(self):
        cmd = self._cmd()
        with patch.object(
            cmd, "_fetch_case_detail", return_value=_case()
        ), patch.object(cmd, "_get_source_parts", return_value=[]), patch.object(
            cmd, "_generate"
        ) as gen:
            out, delta = cmd._process_case(
                case=_case(),
                idx=1,
                total=1,
                dry_run=True,
                force=False,
                skip_title=False,
                benchmark_dir=None,
                llm_cfg={"backend": "bedrock"},
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
            )
        gen.assert_not_called()
        assert delta["cases_no_content"] == 1

    def test_skips_case_with_substantial_description(self):
        # Idempotency: a detail with a >=600-char description is skipped (the
        # list serializer drops `description`, so the skip MUST be at detail).
        cmd = self._cmd()
        detail = _case(description="क" * 700)
        with patch.object(cmd, "_fetch_case_detail", return_value=detail), patch.object(
            cmd, "_generate"
        ) as gen, patch.object(cmd, "_patch_case") as patch_case:
            out, delta = cmd._process_case(
                case=_case(),
                idx=1,
                total=1,
                dry_run=False,
                force=False,
                skip_title=False,
                benchmark_dir=None,
                llm_cfg={"backend": "bedrock"},
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
            )
        gen.assert_not_called()
        patch_case.assert_not_called()
        assert delta["cases_already_populated"] == 1
        # Counted like the list-level skip: already_populated only, NOT processed,
        # so the summary totals reconcile across list/detail skip paths.
        assert "cases_processed" not in delta

    def test_force_regenerates_described_case(self):
        cmd = self._cmd()
        detail = _case(description="क" * 700)
        with patch.object(cmd, "_fetch_case_detail", return_value=detail), patch.object(
            cmd, "_get_source_parts", return_value=[("CIAA_PRESS_RELEASE", "x" * 300)]
        ), patch.object(cmd, "_get_ngm_data", return_value=None), patch.object(
            cmd, "_generate", return_value=self._result()
        ), patch.object(
            cmd, "_patch_case"
        ) as patch_case:
            cmd._process_case(
                case=_case(),
                idx=1,
                total=1,
                dry_run=False,
                force=True,
                skip_title=False,
                benchmark_dir=None,
                llm_cfg={"backend": "bedrock"},
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
            )
        patch_case.assert_called_once()

    def test_skip_title_suppresses_returned_title(self):
        # Even if the model returns a (valid) title, --skip-title must not write it.
        cmd = self._cmd()
        with patch.object(
            cmd, "_fetch_case_detail", return_value=_case()
        ), patch.object(
            cmd, "_get_source_parts", return_value=[("CIAA_PRESS_RELEASE", "x" * 300)]
        ), patch.object(
            cmd, "_get_ngm_data", return_value=None
        ), patch.object(
            cmd, "_generate", return_value=self._result()
        ), patch.object(
            cmd, "_patch_case"
        ) as patch_case:
            cmd._process_case(
                case=_case(),
                idx=1,
                total=1,
                dry_run=False,
                force=False,
                skip_title=True,
                benchmark_dir=None,
                llm_cfg={"backend": "bedrock"},
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
            )
        patch_case.assert_called_once()
        assert patch_case.call_args.kwargs["title"] is None
        assert patch_case.call_args.kwargs["description"] == "### क) सार"

    def test_headcount_title_is_not_patched(self):
        # A regenerated title with a defendant headcount must be suppressed even
        # though it carries the correct court number (description still written).
        cmd = self._cmd()
        bad = {
            "title": "…समेत १२ जना सबैले सफाई (080-CR-0047)",
            "description": "### क) सार",
        }
        with patch.object(
            cmd, "_fetch_case_detail", return_value=_case()
        ), patch.object(
            cmd, "_get_source_parts", return_value=[("CIAA_PRESS_RELEASE", "x" * 300)]
        ), patch.object(
            cmd, "_get_ngm_data", return_value=None
        ), patch.object(
            cmd, "_generate", return_value=bad
        ), patch.object(
            cmd, "_patch_case"
        ) as patch_case:
            cmd._process_case(
                case=_case(),
                idx=1,
                total=1,
                dry_run=False,
                force=False,
                skip_title=False,
                benchmark_dir=None,
                llm_cfg={"backend": "bedrock"},
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
            )
        patch_case.assert_called_once()
        assert patch_case.call_args.kwargs["title"] is None
        assert patch_case.call_args.kwargs["description"] == "### क) सार"


# ── concurrency dispatch ──────────────────────────────────────────────────────


class TestConcurrencyDispatch:
    def _cmd(self):
        # The dispatcher writes buffered output via self.stdout.write(..., ending="");
        # wrap StringIO in Django's OutputWrapper (what management commands use) so
        # the ending kwarg is accepted, matching production.
        from django.core.management.base import OutputWrapper

        cmd = Command()
        self._buf = StringIO()
        cmd.stdout = OutputWrapper(self._buf)
        return cmd

    def _stdout(self):
        return self._buf.getvalue()

    def _fake_process(self, *, case, idx, **kwargs):
        from io import StringIO as _S

        buf = _S()
        buf.write(f"case {case['case_id']} ok\n")
        return buf, {"cases_processed": 1, "cases_enriched": 1}

    def test_serial_processes_all_and_merges_stats(self):
        cmd = self._cmd()
        cases = [_case(case_id=f"case-{i}") for i in range(3)]
        with patch.object(cmd, "_process_case", side_effect=self._fake_process):
            cmd._run_serial(cases, dict(total=3))
        assert cmd.stats["cases_processed"] == 3
        assert cmd.stats["cases_enriched"] == 3
        assert self._stdout().count("ok") == 3

    def test_concurrent_processes_all_and_merges_stats(self):
        cmd = self._cmd()
        cases = [_case(case_id=f"case-{i}") for i in range(5)]
        with patch.object(cmd, "_process_case", side_effect=self._fake_process):
            cmd._run_concurrent(cases, dict(total=5), workers=3)
        assert cmd.stats["cases_processed"] == 5
        assert cmd.stats["cases_enriched"] == 5
        # Every case's buffered output is flushed (atomically per case).
        assert self._stdout().count("ok") == 5

    def test_concurrent_isolates_a_crashing_case(self):
        cmd = self._cmd()
        cases = [_case(case_id=f"case-{i}") for i in range(4)]

        def flaky(*, case, idx, **kwargs):
            if case["case_id"] == "case-1":
                raise RuntimeError("boom")
            return self._fake_process(case=case, idx=idx, **kwargs)

        with patch.object(cmd, "_process_case", side_effect=flaky):
            cmd._run_concurrent(cases, dict(total=4), workers=4)
        # The 3 healthy cases still complete; the crash is counted, not fatal.
        assert cmd.stats["cases_enriched"] == 3
        assert cmd.stats["cases_llm_error"] == 1

    def test_validate_concurrency_rejects_zero(self):
        with pytest.raises(CommandError):
            Command._validate_concurrency(0)

    def test_validate_concurrency_default_is_one(self):
        assert Command._validate_concurrency(None) == 1


# ── PATCH payload ─────────────────────────────────────────────────────────────


class TestPatchCase:
    def test_sends_json_patch(self):
        cmd = Command()
        session = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        session.patch.return_value = resp
        cmd._patch_case(
            case_slug="slug",
            case_id="case-0001",
            description="### क)",
            title="t (080-CR-0047)",
            api_base_url=API_BASE,
            api_token="tok",
            session=session,
        )
        sent = session.patch.call_args.kwargs["json"]
        paths = {op["path"]: op["value"] for op in sent}
        assert paths["/description"] == "### क)"
        assert paths["/title"] == "t (080-CR-0047)"

    def test_omits_title_when_none(self):
        cmd = Command()
        session = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        session.patch.return_value = resp
        cmd._patch_case(
            case_slug="slug",
            case_id="case-0001",
            description="### क)",
            title=None,
            api_base_url=API_BASE,
            api_token="tok",
            session=session,
        )
        sent = session.patch.call_args.kwargs["json"]
        assert [op["path"] for op in sent] == ["/description"]

    def test_missing_slug_raises(self):
        with pytest.raises(CommandError):
            Command()._patch_case(
                case_slug=None,
                case_id="case-0001",
                description="d",
                title=None,
                api_base_url=API_BASE,
                api_token="tok",
                session=MagicMock(),
            )


# ── CLI arguments ─────────────────────────────────────────────────────────────


class TestCliArguments:
    def _run(self, **kwargs):
        opts = {**COMMON, **kwargs}
        call_command("enrich_ciaa_description", stdout=StringIO(), **opts)

    def test_requires_api_token(self):
        with pytest.raises(CommandError):
            with patch.dict("os.environ", {"JAWAFDEHI_API_TOKEN": ""}, clear=False):
                call_command(
                    "enrich_ciaa_description",
                    api_base_url=API_BASE,
                    api_token=None,
                    stdout=StringIO(),
                )

    def test_priority_and_case_id_mutually_exclusive(self):
        with pytest.raises(CommandError):
            self._run(priority=True, case_id="case-0001")

    def test_dry_run_is_default(self):
        # With no --patch, a run over zero cases completes as a dry run.
        cmd = Command()
        with patch.object(cmd, "_get_ciaa_cases", return_value=[]):
            out = StringIO()
            cmd.stdout = out
            call_command(
                cmd,
                api_base_url=API_BASE,
                api_token="tok",
                llm_model="global.anthropic.claude-opus-4-8",
                stdout=out,
            )
            assert "DRY RUN" in out.getvalue()

    def test_backend_auto_detects_bedrock_for_claude_model(self):
        cmd = Command()
        cfg = cmd._resolve_llm_config(
            {
                "llm_model": "global.anthropic.claude-opus-4-8",
                "llm_backend": "auto",
                "llm_max_tokens": 8000,
                "aws_profile": "",
                "aws_region": "us-west-2",
            }
        )
        assert cfg["backend"] == "bedrock"

    def test_openai_backend_requires_llm_key(self):
        cmd = Command()
        with patch.dict(
            "os.environ",
            {"JAWAFDEHI_LLM_API_KEY": "", "ANTHROPIC_API_KEY": ""},
            clear=False,
        ):
            with pytest.raises(CommandError):
                cmd._resolve_llm_config(
                    {
                        "llm_model": "qwen2.5",
                        "llm_backend": "openai",
                        "llm_max_tokens": 8000,
                        "llm_base_url": "https://x/v1",
                        "llm_api_key": None,
                    }
                )
