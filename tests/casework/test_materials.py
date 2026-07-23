# tests/casework/test_materials.py
import urllib.error
import urllib.request

from casework.common.api import BROWSER_UA
from casework.common.materials import (
    court_order_ident, fetch_markdown, markdown_link, material_exists,
    material_iri, materials_of_type, press_release_ident, raw_links, source_text,
)

CASE = {
    "slug": "c1",
    "evidence": [
        {"material_iri": "https://jawafdehi.org/material/ciaa_press_release/1",
         "material": {"material_type": "press_release", "urls": [
             {"link": "https://x/pr.pdf", "role": "RAW"},
             {"link": "https://x/pr.md", "role": "MARKDOWN"}]}},
        {"material_iri": "https://jawafdehi.org/material/court_order/2",
         "material": {"material_type": "court_order", "urls": [
             {"link": "https://x/co.pdf", "role": "RAW"}]}},
    ],
}


def test_markdown_link_found():
    assert markdown_link(CASE["evidence"][0]["material"]) == "https://x/pr.md"


def test_markdown_link_absent_returns_none():
    assert markdown_link(CASE["evidence"][1]["material"]) is None


def test_raw_links_collected():
    assert raw_links(CASE["evidence"][1]["material"]) == ["https://x/co.pdf"]


def test_materials_of_type_filters():
    got = materials_of_type(CASE, ("press_release",))
    assert len(got) == 1
    assert got[0]["material_type"] == "press_release"


def test_source_text_reports_unmet_when_no_markdown(monkeypatch):
    import casework.common.materials as m
    monkeypatch.setattr(m, "fetch_markdown", lambda link: "प्रेस विज्ञप्ति पाठ")
    text, unmet = source_text(CASE, api=None, types=("press_release", "court_order"))
    assert "प्रेस विज्ञप्ति पाठ" in text
    # court_order has RAW but no MARKDOWN -- must be REPORTED, not silently dropped.
    assert any("court_order" in u for u in unmet)


def test_source_text_reports_fetched_but_blank_markdown(monkeypatch):
    # The fixture MUST carry a MARKDOWN role. An earlier version used a
    # RAW-only material, which short-circuits at `if not link` and never
    # reaches the blank-document branch at all -- mutation testing showed
    # that branch had zero coverage while the test appeared to cover it.
    import casework.common.materials as m
    monkeypatch.setattr(m, "fetch_markdown", lambda link: "   ")
    case = {"slug": "c", "evidence": [
        {"material_iri": "i", "material": {"material_type": "court_order",
         "urls": [{"link": "https://x/a.md", "role": "MARKDOWN"}]}}]}
    text, unmet = source_text(case, api=None, types=("court_order",))
    assert text == ""
    assert any("empty" in u for u in unmet)


def test_source_text_reports_fetch_failure(monkeypatch):
    import casework.common.materials as m

    def _raise(link):
        raise OSError("boom")

    monkeypatch.setattr(m, "fetch_markdown", _raise)
    case = {"slug": "c", "evidence": [
        {"material_iri": "i", "material": {"material_type": "court_order",
         "urls": [{"link": "https://x/a.md", "role": "MARKDOWN"}]}}]}
    text, unmet = source_text(case, api=None, types=("court_order",))
    assert text == ""
    assert any("fetch failed" in u for u in unmet)


def test_fetch_markdown_sends_browser_user_agent(monkeypatch):
    # The upstream WAF 403s the stdlib default `Python-urllib/3.x` UA (Task 16
    # A/B: this silently dropped 8.1% of cases' source text). Assert the
    # actual header on the request object -- not merely that BROWSER_UA
    # exists somewhere in the module.
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return "content".encode("utf-8")

    def fake_urlopen(req, timeout=None):
        captured["request"] = req
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    text = fetch_markdown("https://x/a.md")

    assert text == "content"
    req = captured["request"]
    assert isinstance(req, urllib.request.Request)
    ua = req.get_header("User-agent")
    assert ua == BROWSER_UA
    assert not (ua or "").startswith("Python-urllib")


def test_source_text_reports_unresolved_material():
    # material:null == a LIST-endpoint payload. Must be loudly unmet, never
    # a silent ("", []) that reads as "this case has no evidence".
    case = {"slug": "c", "evidence": [{"material_iri": "i", "material": None}]}
    text, unmet = source_text(case, api=None, types=("court_order",))
    assert text == ""
    assert any("UNRESOLVED" in u for u in unmet)


# ---------------------------------------------------------------------------
# Binder-facing helpers: IRI builders + the existence probe. Court-order idents
# MUST be lowercased (uppercase -> HTTP 400); the probe classifies 200 / absent
# / uncertain, and the caller must never bind on `None`.
# ---------------------------------------------------------------------------


def test_material_iri_builds_canonical_at_id():
    assert (material_iri("ciaa_press_release", "2037")
            == "https://jawafdehi.org/material/ciaa_press_release/2037")


def test_press_release_ident_is_the_release_id():
    assert press_release_ident(2037) == "2037"
    assert press_release_ident("  2037 ") == "2037"


def test_court_order_ident_lowercases_the_case_number():
    # uppercase would 400 server-side; the builder must lowercase.
    assert court_order_ident("special", "078-CR-0042") == "special.078-cr-0042"
    assert court_order_ident("supreme", " 079-CR-0001 ") == "supreme.079-cr-0001"


class _StubApi:
    """Minimal stand-in for CaseworkApi.get: returns or raises per script."""
    def __init__(self, outcome):
        self._outcome = outcome
        self.calls = []

    def get(self, path, timeout=60):
        self.calls.append(path)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def test_material_exists_true_on_200():
    api = _StubApi({"@id": "x"})
    assert material_exists(api, "ciaa_press_release", "2037") is True
    assert api.calls == ["/materials/ciaa_press_release/2037/"]


def test_material_exists_false_on_404():
    err = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    assert material_exists(_StubApi(err), "court_order", "special.078-cr-9999") is False


def test_material_exists_false_on_400():
    err = urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
    assert material_exists(_StubApi(err), "court_order", "SPECIAL.078-CR-1") is False


def test_material_exists_none_on_500():
    err = urllib.error.HTTPError("u", 500, "Server Error", {}, None)
    assert material_exists(_StubApi(err), "ag", "86115") is None


def test_material_exists_none_on_transport_error():
    assert material_exists(_StubApi(urllib.error.URLError("refused")), "ag", "1") is None


def test_material_exists_percent_encodes_the_ident():
    api = _StubApi({"ok": 1})
    material_exists(api, "court_order", "special.078-cr-0042")
    # dots are safe, but the ident is quoted with safe='' so slashes/space encode
    assert api.calls[0] == "/materials/court_order/special.078-cr-0042/"


# ---------------------------------------------------------------------------
# probe_material -- richer than material_exists: also carries the HTTP status
# and the probed path, for the binder's per-material audit log line.
# ---------------------------------------------------------------------------


def test_probe_material_carries_status_path_and_verdict_on_200():
    from casework.common.materials import probe_material
    pr = probe_material(_StubApi({"@id": "x"}), "ciaa_press_release", "2037")
    assert (pr.source, pr.ident, pr.status, pr.verdict) == (
        "ciaa_press_release", "2037", 200, True)
    assert pr.path == "/materials/ciaa_press_release/2037/"


def test_probe_material_reports_404_status_and_absent_verdict():
    from casework.common.materials import probe_material
    err = urllib.error.HTTPError("u", 404, "nf", {}, None)
    pr = probe_material(_StubApi(err), "court_order", "special.078-cr-9999")
    assert pr.status == 404 and pr.verdict is False


def test_probe_material_reports_none_status_on_transport_error():
    from casework.common.materials import probe_material
    pr = probe_material(_StubApi(urllib.error.URLError("x")), "ag", "1")
    assert pr.status is None and pr.verdict is None


def test_probe_material_still_probes_once_with_a_negative_retry_count():
    # argparse accepts `--probe-retries -1`; range(-1 + 1) is EMPTY, so this
    # used to return the None initializer and every caller's `.verdict`
    # raised AttributeError.
    from casework.common.materials import material_exists, probe_material
    pr = probe_material(_StubApi({"@id": "x"}), "ag", "1", retries=-1)
    assert pr is not None and pr.verdict is True
    assert material_exists(_StubApi({"@id": "x"}), "ag", "1") is True


def test_backoff_never_returns_a_negative_sleep():
    # time.sleep() raises ValueError on a negative argument, so a negative
    # interval would abort the walk with a traceback rather than degrade.
    from casework.common.materials import _backoff_s
    assert _backoff_s(None, 0, -5) == 0
    assert _backoff_s(-3, 0, 1.0) == 0
    assert _backoff_s(None, 3, 1.0) == 8


def test_probe_material_survives_a_negative_interval():
    # The uncertain path is the one that sleeps; it must not raise.
    from casework.common.materials import probe_material
    err = urllib.error.HTTPError("u", 503, "boom", {}, None)
    pr = probe_material(_StubApi(err), "ag", "1", retries=2, interval=-1)
    assert pr.status == 503 and pr.verdict is None
