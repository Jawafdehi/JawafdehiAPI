import sys
from types import SimpleNamespace

import requests

from cases.management.commands import enrich_ciaa_allegations
from cases.management.commands.enrich_ciaa_allegations import Command
from cases.models import Case


class FakeResponse:
    url = "https://ciaa.gov.np/example.pdf"
    headers = {"content-type": "application/pdf"}
    text = ""
    encoding = None

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        del chunk_size
        yield b"%PDF-1.7"


class FakeTextResponse(FakeResponse):
    headers = {"content-type": "text/plain"}
    text = "x" * 201


class FakeUntrustedResponse(FakeResponse):
    url = "https://example.com/example.pdf"


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
    assert calls == ["closed"]


def test_convert_to_markdown_closes_old_db_connections_on_text_return(monkeypatch):
    calls = []

    monkeypatch.setattr(
        enrich_ciaa_allegations.requests,
        "get",
        lambda *args, **kwargs: FakeTextResponse(),
    )
    monkeypatch.setattr(
        enrich_ciaa_allegations,
        "close_old_connections",
        lambda: calls.append("closed"),
    )

    result = Command()._convert_to_markdown("https://ciaa.gov.np/example.txt")

    assert result == "x" * 201
    assert calls == ["closed"]


def test_convert_to_markdown_closes_old_db_connections_on_untrusted_redirect(
    monkeypatch,
):
    calls = []

    monkeypatch.setattr(
        enrich_ciaa_allegations.requests,
        "get",
        lambda *args, **kwargs: FakeUntrustedResponse(),
    )
    monkeypatch.setattr(
        enrich_ciaa_allegations,
        "close_old_connections",
        lambda: calls.append("closed"),
    )

    result = Command()._convert_to_markdown("https://ciaa.gov.np/example.pdf")

    assert result is None
    assert calls == ["closed"]


def test_convert_to_markdown_closes_old_db_connections_on_download_error(monkeypatch):
    calls = []

    def raise_timeout(*args, **kwargs):
        raise requests.Timeout("timeout")

    monkeypatch.setattr(enrich_ciaa_allegations.requests, "get", raise_timeout)
    monkeypatch.setattr(
        enrich_ciaa_allegations,
        "close_old_connections",
        lambda: calls.append("closed"),
    )

    result = Command()._convert_to_markdown("https://ciaa.gov.np/example.pdf")

    assert result is None
    assert calls == ["closed"]


def test_process_case_closes_old_db_connections_after_llm_before_save(monkeypatch):
    calls = []
    command = Command()
    command.stats = {
        "cases_processed": 0,
        "cases_no_content": 0,
        "cases_llm_error": 0,
        "cases_skipped": 0,
        "cases_enriched": 0,
    }
    command.stdout = SimpleNamespace(write=lambda *args, **kwargs: None)
    command.style = SimpleNamespace(
        WARNING=lambda value: value,
        ERROR=lambda value: value,
        SUCCESS=lambda value: value,
    )

    monkeypatch.setattr(command, "_get_press_release_content", lambda case: "x" * 201)
    monkeypatch.setattr(
        command,
        "_extract_allegations",
        lambda **kwargs: ["first allegation", "second allegation"],
    )
    monkeypatch.setattr(command, "_save_allegations", lambda case, allegations: None)
    monkeypatch.setattr(
        enrich_ciaa_allegations,
        "close_old_connections",
        lambda: calls.append("closed"),
    )

    command._process_case(
        case=Case(case_id="CASE-1", title="Case title"),
        idx=1,
        total=1,
        dry_run=False,
        llm_model="model",
        llm_base_url="https://llm.example.com",
        llm_api_key="key",
    )

    assert calls == ["closed"]
    assert command.stats["cases_enriched"] == 1
