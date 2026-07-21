"""End-to-end write-guard wiring test: `main()` -> real `build_api()` -> real
`CaseworkApi` -> the guard in `CaseworkApi._patch`.

Every other enricher test monkeypatches `build_api` itself (a `_StubApi`
stands in for `CaseworkApi`), which is the right choice for testing extraction
logic but means those tests never actually exercise the guard wiring added in
this task. This file is the one place that runs `main()` with a REAL
`CaseworkApi` end to end and asserts the write-guard -- not a stub -- is what
stops the PATCH.

HARD SAFETY RULE: this must never touch the real `api.jawafdehi.org`, not even
a request that gets rejected -- a 401 is still a request that left the
machine. `urllib.request.urlopen` is monkeypatched at the top of the urllib
call stack (the same seam `casework/common/api.py`'s own `_request` and
`casework/common/materials.py`'s `fetch_markdown` go through), so NOTHING
this test does ever opens a socket. The non-loopback host used throughout is
`https://example.invalid` (RFC 2606 reserved -- guaranteed never to resolve),
never the real production host.
"""
import json
import sys
import types
import urllib.request

import pytest

from casework import enrich_missing_bigo as emb

NON_LOOPBACK_BASE_URL = "https://example.invalid"


class _FakeHTTPResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_llm_modules(invoke_text_stub):
    class _FakeUsage:
        calls = 0

        def as_dict(self):
            return {"by_provider": []}

    fake_invoke = types.ModuleType("llm.invoke")
    fake_invoke.invoke_text = invoke_text_stub

    fake_usage = types.ModuleType("llm.usage")
    fake_usage.UsageAccumulator = _FakeUsage
    fake_usage.render_usage_table = lambda by_provider, title=None: ""
    return fake_invoke, fake_usage


def _install_fake_urlopen(monkeypatch, calls, *, list_body, detail_body, detail_path):
    """Route GET /cases/ (list) and GET <detail_path> to canned JSON bodies;
    fail the test immediately (never touch urlopen) if a PATCH is attempted --
    the guard must raise INSIDE `CaseworkApi._patch`, before `_request` is
    ever called, so a PATCH reaching this fake would itself prove the guard
    did not fire.
    """

    def fake_urlopen(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        calls.append((method, url))
        if method == "PATCH":
            pytest.fail(
                "guard must block this PATCH before urlopen is ever called -- "
                "a request reaching here means the write-guard did not fire"
            )
        if detail_path in url:
            return _FakeHTTPResponse(body=detail_body)
        return _FakeHTTPResponse(body=list_body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


CASE_SLUG = "case-guard-wiring"

LIST_BODY = json.dumps({
    "results": [{"slug": CASE_SLUG, "title": "बिगो रु. १०,००० कायम", "state": "DRAFT"}],
    "next": None,
}).encode()

DETAIL_BODY = json.dumps({
    "slug": CASE_SLUG,
    "title": "बिगो रु. १०,००० कायम",
    "bigo": None,
    "court_cases": [],
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa/press_releases/1",
         "material": {"material_type": "press_release",
                      "urls": [{"link": "https://x/1.md", "role": "MARKDOWN"}]}},
    ],
}).encode()

BIGO_RESPONSE = json.dumps({
    "bigo": 10000, "confidence": "high",
    "evidence_quote": "बिगो रु. १०,००० कायम भएको छ",
})


def test_apply_without_allow_remote_writes_blocks_patch_end_to_end(monkeypatch):
    """`--apply` + a non-loopback `--api-base-url` + NO `--allow-remote-writes`
    must: (1) never call urlopen with PATCH, (2) record the guard's
    RuntimeError as a `write`/`error` outcome, not silently swallow it.
    """
    calls = []
    _install_fake_urlopen(
        monkeypatch, calls, list_body=LIST_BODY, detail_body=DETAIL_BODY,
        detail_path=f"/cases/{CASE_SLUG}/",
    )
    monkeypatch.setattr(emb, "bootstrap", lambda *a, **k: None)
    import casework.common.materials as materials_mod
    monkeypatch.setattr(
        materials_mod, "fetch_markdown",
        lambda link, timeout=60: "बिगो रु. १०,००० कायम भएको छ ।",
    )
    fake_invoke, fake_usage = _fake_llm_modules(lambda **kw: BIGO_RESPONSE)
    monkeypatch.setitem(sys.modules, "llm.invoke", fake_invoke)
    monkeypatch.setitem(sys.modules, "llm.usage", fake_usage)

    report = emb.main([
        "--api-base-url", NON_LOOPBACK_BASE_URL,
        "--api-token", "test-token",
        "--apply", "--slug", CASE_SLUG,
    ])

    # No PATCH ever reached urlopen -- only the GETs (list + detail).
    assert all(method != "PATCH" for method, _ in calls)
    assert any(method == "GET" for method, _ in calls)

    # The guard's RuntimeError was caught and recorded, not swallowed silently.
    error_rows = [r for r in report.rows if r["status"] == "error"]
    assert error_rows, f"expected a recorded error row, got: {report.rows}"
    assert any("refusing to write" in r["reason"] for r in error_rows)
    assert any("example.invalid" in r["reason"] for r in error_rows)


def test_apply_with_allow_remote_writes_lets_the_patch_reach_urlopen(monkeypatch):
    """Sanity check on the other side of the flag: WITH
    `--allow-remote-writes`, the same run's PATCH is allowed to reach
    `urlopen` (still never a real network call -- `urlopen` itself is
    monkeypatched throughout this test, so nothing leaves the machine)."""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append((req.get_method(), req.full_url))
        if req.get_method() == "PATCH":
            return _FakeHTTPResponse(body=b"{}")
        if f"/cases/{CASE_SLUG}/" in req.full_url:
            return _FakeHTTPResponse(body=DETAIL_BODY)
        return _FakeHTTPResponse(body=LIST_BODY)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(emb, "bootstrap", lambda *a, **k: None)
    import casework.common.materials as materials_mod
    monkeypatch.setattr(
        materials_mod, "fetch_markdown",
        lambda link, timeout=60: "बिगो रु. १०,००० कायम भएको छ ।",
    )
    fake_invoke, fake_usage = _fake_llm_modules(lambda **kw: BIGO_RESPONSE)
    monkeypatch.setitem(sys.modules, "llm.invoke", fake_invoke)
    monkeypatch.setitem(sys.modules, "llm.usage", fake_usage)

    report = emb.main([
        "--api-base-url", NON_LOOPBACK_BASE_URL,
        "--api-token", "test-token",
        "--apply", "--allow-remote-writes", "--slug", CASE_SLUG,
    ])

    assert any(method == "PATCH" for method, _ in calls)
    assert any(r["status"] == "enriched" for r in report.rows)
