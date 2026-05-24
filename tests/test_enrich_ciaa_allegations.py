import sys
from types import SimpleNamespace

from cases.management.commands import enrich_ciaa_allegations
from cases.management.commands.enrich_ciaa_allegations import Command


class FakeResponse:
    url = "https://ciaa.gov.np/example.pdf"
    headers = {"content-type": "application/pdf"}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        del chunk_size
        yield b"%PDF-1.7"


class FakeMarkItDown:
    def __init__(self, enable_plugins):
        self.enable_plugins = enable_plugins

    def convert(self, path):
        assert path
        return SimpleNamespace(text_content="x" * 201)


def test_convert_to_markdown_closes_old_db_connections(monkeypatch):
    calls = []

    monkeypatch.setattr(
        enrich_ciaa_allegations.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(),
    )
    monkeypatch.setattr(
        enrich_ciaa_allegations,
        "close_old_connections",
        lambda: calls.append("closed"),
    )
    monkeypatch.setitem(sys.modules, "likhit", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "markitdown",
        SimpleNamespace(MarkItDown=FakeMarkItDown),
    )

    result = Command()._convert_to_markdown("https://ciaa.gov.np/example.pdf")

    assert result == "x" * 201
    assert calls == ["closed", "closed"]
