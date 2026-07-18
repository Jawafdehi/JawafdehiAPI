"""Import-smoke for the registry + command wiring (surfaces import errors)."""
from courts.scraper import registry


def test_registry_has_all_four_courts():
    assert set(registry.REGISTRY) == {"special", "district", "high", "supreme"}


def test_resolve_all_and_single_and_unknown():
    assert set(registry.resolve("all")) == {"special", "district", "high", "supreme"}
    assert registry.resolve("special") == ["special"]
    import pytest
    with pytest.raises(KeyError):
        registry.resolve("bogus")


def test_all_specs_expose_crawl_detail():
    # Every court has a detail parser, so every registry spec must wire
    # crawl_detail — otherwise that court's `--enrich` is silently a no-op
    # (regression guard for the unreachable high-court enrichment).
    for key, spec in registry.REGISTRY.items():
        assert hasattr(spec, "crawl_detail"), f"{key} spec missing crawl_detail"


def test_court_ids_counts():
    assert registry.REGISTRY["supreme"].court_ids(None) == ["supreme"]
    assert len(registry.REGISTRY["district"].court_ids(None)) >= 70
    assert len(registry.REGISTRY["high"].court_ids(None)) >= 15


def test_command_imports():
    from courts.management.commands.scrape_courtcases import Command
    assert Command.help


class _FakeResp:
    """Minimal requests.Response stand-in: .text reflects the current .encoding."""

    def __init__(self, content_type, by_encoding, encoding="ISO-8859-1"):
        self.headers = {"content-type": content_type}
        self.encoding = encoding
        self._by_encoding = by_encoding

    @property
    def text(self):
        return self._by_encoding.get(self.encoding, "<mojibake>")


def test_decode_forces_utf8_when_portal_omits_charset():
    # The real bug: portal serves UTF-8 Devanagari with no charset → requests
    # defaults to ISO-8859-1 and mojibakes it. _decode must override to UTF-8.
    from courts.management.commands.scrape_courtcases import _decode

    resp = _FakeResp("text/html", {"utf-8-sig": "नेपाल सरकार", "ISO-8859-1": "à¤¨à¥‡à¤ª"})
    assert _decode(resp) == "नेपाल सरकार"
    assert resp.encoding == "utf-8-sig"


def test_decode_honors_explicit_header_charset():
    from courts.management.commands.scrape_courtcases import _decode

    resp = _FakeResp("text/html; charset=utf-8", {"utf-8": "फैसला"}, encoding="utf-8")
    assert _decode(resp) == "फैसला"
    assert resp.encoding == "utf-8"  # left untouched
