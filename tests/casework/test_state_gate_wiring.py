"""End-to-end state-gate wiring: every enricher `main()` -> `select_cases`.

`casework/common/select.py` is where "bulk enrichment is DRAFT-only" is
decided, and `tests/casework/test_select.py` pins that function's behaviour.
This file pins the other half -- that all five enrichers actually *reach* it
with the right `states`. A `main()` that dropped `states=(args.state,)` from
its `select_cases(...)` call would still pass every test in `test_select.py`
while quietly falling back to whatever the module default happens to be, so
the wiring needs its own mutation-sensitive coverage.

NO NETWORK: `build_api` is replaced with a stub that returns a fake client, so
no `CaseworkApi` is constructed and `urlopen` is never reached. `bootstrap`
and the `llm.*` modules are stubbed too, so no LLM provider is invoked.
"""
import sys
import types

import pytest

from casework import enrich_allegations as c_allegations
from casework import enrich_missing_bigo as c_bigo
from casework import enrich_related_entities as c_entities
from casework import enrich_tags as c_tags
from casework import enrich_timeline as c_timeline

ENRICHERS = [c_bigo, c_tags, c_timeline, c_allegations, c_entities]

# Loopback: `main()` never makes a request here (the API is stubbed), but the
# base URL still has to be one this project would tolerate.
LOOPBACK_BASE_URL = "http://127.0.0.1:48010"

SPECIAL = "https://jawafdehi.org/courtcase/special/081-cr-0098"


class _StubApi:
    """Stands in for `CaseworkApi`. Only `iter_cases` is ever reached: every
    test here selects zero cases, so `main()` returns before touching the
    per-case read/write methods."""

    def __init__(self, cases):
        self._cases = cases

    def iter_cases(self):
        return iter(self._cases)


def _stub_runtime(monkeypatch, module, cases):
    """Neutralise everything `main()` does before selection: Django/LLM
    bootstrap, the `llm.*` imports it performs at runtime, and API
    construction."""
    monkeypatch.setattr(module, "bootstrap", lambda *a, **k: None)
    monkeypatch.setattr(module, "build_api", lambda args: _StubApi(cases))

    fake_invoke = types.ModuleType("llm.invoke")
    fake_invoke.invoke_text = lambda **kw: pytest.fail(
        "no case should have been selected, so no LLM call should happen")
    # `enrich_timeline` imports this name too; the others do not.
    fake_invoke.invoke_with_tools = fake_invoke.invoke_text

    class _FakeUsage:
        def as_dict(self):
            return {"by_provider": []}

    fake_usage = types.ModuleType("llm.usage")
    fake_usage.UsageAccumulator = _FakeUsage
    fake_usage.render_usage_table = lambda by_provider, title=None: ""

    monkeypatch.setitem(sys.modules, "llm.invoke", fake_invoke)
    monkeypatch.setitem(sys.modules, "llm.usage", fake_usage)


def _record_select_cases(monkeypatch, module):
    """Replace `module.select_cases` with a recorder that selects nothing, so
    `main()` returns immediately after selection. Returns the kwargs dict,
    populated by the call."""
    captured = {}

    def recorder(cases, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(module, "select_cases", recorder)
    return captured


@pytest.mark.parametrize("module", ENRICHERS, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
class TestStateGateReachesSelectCases:

    def test_bulk_run_asks_for_draft_by_default(self, module, monkeypatch):
        """A bare bulk run must gate on DRAFT -- not on the old
        `("DRAFT", "IN_REVIEW")` pair, and not on "whatever `select_cases`
        defaults to" either. The default is asserted as an exact tuple so
        adding IN_REVIEW back anywhere in the chain fails here."""
        _stub_runtime(monkeypatch, module, cases=[])
        captured = _record_select_cases(monkeypatch, module)

        module.main(["--api-base-url", LOOPBACK_BASE_URL, "--dry-run"])

        assert captured["states"] == ("DRAFT",)

    def test_state_flag_is_threaded_through(self, module, monkeypatch):
        """`--state` is not decorative: it reaches the selector."""
        _stub_runtime(monkeypatch, module, cases=[])
        captured = _record_select_cases(monkeypatch, module)

        module.main([
            "--api-base-url", LOOPBACK_BASE_URL, "--dry-run",
            "--state", "PUBLISHED",
        ])

        assert captured["states"] == ("PUBLISHED",)

    def test_bulk_run_selects_no_in_review_case(self, module, monkeypatch):
        """The real `select_cases`, a corpus of nothing but IN_REVIEW cases:
        the run must select zero. Enriching a case a moderator already has
        open for review is a scope violation, and this is the path that used
        to do it on every bulk run."""
        in_review = [
            {"slug": f"review-{i}", "state": "IN_REVIEW", "court_cases": [SPECIAL]}
            for i in range(3)
        ]
        _stub_runtime(monkeypatch, module, cases=in_review)

        report = module.main(["--api-base-url", LOOPBACK_BASE_URL, "--dry-run"])

        assert report.rows == []

    def test_bulk_run_refuses_an_explicit_in_review_state(self, module, monkeypatch):
        """`--state IN_REVIEW` must fail loud rather than enrich the review
        queue. It surfaces as the `ValueError` `select_cases` raises -- an
        unhandled crash before any case is touched, which is the intended
        outcome: nothing quietly proceeds."""
        in_review = [{"slug": "r", "state": "IN_REVIEW", "court_cases": [SPECIAL]}]
        _stub_runtime(monkeypatch, module, cases=in_review)

        with pytest.raises(ValueError, match="IN_REVIEW"):
            module.main([
                "--api-base-url", LOOPBACK_BASE_URL, "--dry-run",
                "--state", "IN_REVIEW",
            ])
