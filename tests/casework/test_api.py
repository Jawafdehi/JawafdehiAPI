import base64
import json
import logging
import urllib.error
import urllib.request

import pytest

from casework.common.api import CaseworkApi, build_replace_patch


def test_build_replace_patch_is_a_list_of_ops():
    patch = build_replace_patch("bigo", 10403941)
    assert isinstance(patch, list)
    assert patch == [{"op": "replace", "path": "/bigo", "value": 10403941}]


def test_build_replace_patch_handles_list_paths():
    items = [{"material_iri": "https://jawafdehi.org/material/news/1",
              "additional_details": ""}]
    assert build_replace_patch("evidence", items) == [
        {"op": "replace", "path": "/evidence", "value": items}
    ]


def test_patch_uses_plain_application_json(monkeypatch):
    seen = {}

    def fake_request(method, url, data=None, headers=None, timeout=None):
        seen.update(method=method, url=url, data=data, headers=headers)
        class R:
            status = 200
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()

    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "_request", fake_request)
    api.patch_field("some-slug", "bigo", 500)

    assert seen["method"] == "PATCH"
    # 415 if this is application/json-patch+json -- no such parser is registered.
    assert seen["headers"]["Content-Type"] == "application/json"
    assert isinstance(json.loads(seen["data"].decode()), list)


# ---------------------------------------------------------------------------
# Auth mode: Bearer (production, default) vs Basic (local DEV_AUTH only).
#
# OIDCAuthentication runs FIRST in DRF's authenticator chain and treats any
# `Bearer` header as its own to authenticate (and 401s it when OIDC_JWKS_URI/
# OIDC_ISSUER are unset, as they are by default under local sqlite dev). DRF's
# additive DEV_AUTH authenticators (SessionAuthentication/BasicAuthentication)
# only ever get a turn for a request that does NOT carry a Bearer header, so a
# local writer must send `Authorization: Basic ...` instead -- never both.
# ---------------------------------------------------------------------------


def _fake_request_capturing(seen):
    def fake_request(method, url, data=None, headers=None, timeout=None):
        seen.update(method=method, url=url, data=data, headers=headers)

        class R:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()

    return fake_request


def test_bearer_mode_sends_bearer_header_and_no_basic(monkeypatch):
    seen = {}
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="prod-token")
    monkeypatch.setattr(api, "_request", _fake_request_capturing(seen))

    api.patch_field("some-slug", "bigo", 500)

    auth = seen["headers"]["Authorization"]
    assert auth == "Bearer prod-token"
    assert not auth.startswith("Basic ")


def test_basic_mode_sends_basic_header_and_no_bearer(monkeypatch):
    seen = {}
    api = CaseworkApi(base_url="http://127.0.0.1:48010", basic=("abgen", "local-dev-only"))
    monkeypatch.setattr(api, "_request", _fake_request_capturing(seen))

    api.patch_field("some-slug", "bigo", 500)

    auth = seen["headers"]["Authorization"]
    assert not auth.startswith("Bearer ")
    assert auth.startswith("Basic ")
    decoded = base64.b64decode(auth.removeprefix("Basic ")).decode()
    assert decoded == "abgen:local-dev-only"


def test_bearer_and_basic_are_mutually_exclusive():
    with pytest.raises(ValueError):
        CaseworkApi(
            base_url="http://127.0.0.1:48010",
            token="prod-token",
            basic=("abgen", "local-dev-only"),
        )


def test_bearer_or_basic_is_required():
    with pytest.raises(ValueError):
        CaseworkApi(base_url="http://127.0.0.1:48010")


def test_basic_mode_rejects_non_loopback_base_url():
    with pytest.raises(ValueError):
        CaseworkApi(
            base_url="https://example.invalid",
            basic=("abgen", "local-dev-only"),
        )


def test_basic_mode_accepts_loopback_base_url():
    # Must not raise -- basic= against 127.0.0.1/localhost is the whole point
    # of DEV_AUTH local writes.
    CaseworkApi(base_url="http://127.0.0.1:48010", basic=("abgen", "local-dev-only"))
    CaseworkApi(base_url="http://localhost:48010", basic=("abgen", "local-dev-only"))


# ---------------------------------------------------------------------------
# replace_list -- the highest-risk method: the server deletes every join row
# and recreates from exactly what is sent, so a partial list silently
# destroys the omitted rows.
# ---------------------------------------------------------------------------


def test_replace_list_emits_single_replace_op_for_evidence(monkeypatch):
    seen = {}
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "_request", _fake_request_capturing(seen))

    items = [{"material_iri": "https://jawafdehi.org/material/news/1",
              "additional_details": ""}]
    api.replace_list("some-slug", "evidence", items)

    body = json.loads(seen["data"].decode())
    assert body == [{"op": "replace", "path": "/evidence", "value": items}]


def test_replace_list_emits_single_replace_op_for_entities(monkeypatch):
    seen = {}
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "_request", _fake_request_capturing(seen))

    items = [{"entity_iri": "https://jawafdehi.org/entity/nes/1"}]
    api.replace_list("some-slug", "entities", items)

    body = json.loads(seen["data"].decode())
    assert body == [{"op": "replace", "path": "/entities", "value": items}]


def test_replace_list_rejects_non_whole_list_path():
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    with pytest.raises(ValueError):
        api.replace_list("some-slug", "bigo", [1, 2, 3])


# ---------------------------------------------------------------------------
# get_case -- must hit the detail endpoint (the only one that resolves
# `material` objects on evidence; the list endpoint returns `material: null`)
# and URL-quote the slug.
# ---------------------------------------------------------------------------


def test_get_case_requests_detail_endpoint_and_quotes_slug(monkeypatch):
    seen = {}

    def fake_request(method, url, data=None, headers=None, timeout=None):
        seen.update(method=method, url=url, data=data, headers=headers)

        class R:
            status = 200

            def read(self):
                return b'{"slug": "a slug with?special"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()

    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "_request", fake_request)

    api.get_case("a slug with?special")

    assert seen["method"] == "GET"
    assert seen["url"] == "http://127.0.0.1:48010/api/cases/a%20slug%20with%3Fspecial/"


# ---------------------------------------------------------------------------
# iter_cases -- must follow pagination until a page has no `next`.
# ---------------------------------------------------------------------------


def test_iter_cases_follows_pagination(monkeypatch):
    pages = {
        1: {"results": [{"slug": "case-a"}, {"slug": "case-b"}], "next": "http://x/?page=2"},
        2: {"results": [{"slug": "case-c"}], "next": None},
    }
    seen_pages = []

    def fake_get(path, params=None, timeout=60):
        page = params["page"]
        seen_pages.append(page)
        return pages[page]

    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "get", fake_get)

    cases = list(api.iter_cases())

    assert [c["slug"] for c in cases] == ["case-a", "case-b", "case-c"]
    assert seen_pages == [1, 2]


# ---------------------------------------------------------------------------
# Write-guard -- `_patch` is the single choke point for `patch_field` and
# `replace_list`. It must refuse to fire a PATCH at any non-loopback host
# unless `allow_remote_writes=True` was explicitly passed to `__init__`.
# Reads (`get`, `iter_cases`, `get_case`) must NEVER be guarded.
# ---------------------------------------------------------------------------


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


def _urlopen_spy(calls, status=200, body=b"{}"):
    def _urlopen(req, timeout=None):
        calls.append(req)
        return _FakeHTTPResponse(status=status, body=body)
    return _urlopen


def _failing_urlopen(*a, **k):
    pytest.fail("urlopen must not be called when the write-guard blocks the request")


def test_patch_field_raises_for_non_loopback_without_opt_in(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _failing_urlopen)
    api = CaseworkApi(base_url="https://example.invalid", token="t")

    with pytest.raises(RuntimeError, match="example.invalid"):
        api.patch_field("some-slug", "bigo", 500)


def test_replace_list_raises_for_non_loopback_without_opt_in(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _failing_urlopen)
    api = CaseworkApi(base_url="https://example.invalid", token="t")

    with pytest.raises(RuntimeError, match="example.invalid"):
        api.replace_list("some-slug", "evidence", [])


def test_patch_field_allowed_for_non_loopback_with_opt_in(monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_spy(calls))
    api = CaseworkApi(
        base_url="https://example.invalid", token="t", allow_remote_writes=True
    )

    api.patch_field("some-slug", "bigo", 500)  # must not raise

    assert len(calls) == 1


def test_patch_field_allowed_for_loopback_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_spy(calls))
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")

    api.patch_field("some-slug", "bigo", 500)  # must not raise

    assert len(calls) == 1


def test_get_is_never_guarded_against_non_loopback(monkeypatch):
    calls = []
    monkeypatch.setattr(
        urllib.request, "urlopen", _urlopen_spy(calls, body=b'{"slug": "x"}')
    )
    api = CaseworkApi(base_url="https://example.invalid", token="t")

    api.get_case("some-slug")  # must not raise -- reads are unguarded

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Write-guard at the TRUE choke point -- `_request` itself.
#
# `_patch` claiming to be "the single choke point for every write" was false:
# `convert.py:188` writes via `api._request("POST", ...)` directly, bypassing
# `_patch` and its guard entirely. The guard now lives in `_request`, so it
# covers POST (and any future write path) as well as PATCH. These tests call
# `_request` directly -- not through `patch_field`/`replace_list` -- to prove
# the guard fires at that layer specifically, independent of `_patch`'s own
# (now redundant) copy of the same check.
# ---------------------------------------------------------------------------


def test_request_post_raises_for_non_loopback_without_opt_in(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _failing_urlopen)
    api = CaseworkApi(base_url="https://example.invalid", token="t")

    with pytest.raises(RuntimeError, match="example.invalid"):
        api._request("POST", "https://example.invalid/api/materials/x/y/file",
                     data=b"{}", headers=api._headers())


def test_request_post_allowed_for_non_loopback_with_opt_in(monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_spy(calls))
    api = CaseworkApi(
        base_url="https://example.invalid", token="t", allow_remote_writes=True
    )

    with api._request("POST", "https://example.invalid/api/materials/x/y/file",
                      data=b"{}", headers=api._headers()):
        pass

    assert len(calls) == 1


def test_request_get_is_never_guarded_against_non_loopback(monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_spy(calls))
    api = CaseworkApi(base_url="https://example.invalid", token="t")

    with api._request("GET", "https://example.invalid/api/cases/",
                      headers=api._headers()):
        pass  # must not raise -- reads are unguarded, even at this layer

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Per-request logging -- `_request` is the single method all HTTP goes
# through. GET (reads) log at DEBUG, PATCH/POST (writes) log at INFO. No
# Authorization header, bearer token, or Basic credentials may ever reach a
# log record.
# ---------------------------------------------------------------------------


def test_get_logs_at_debug_with_status_and_elapsed(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(
        urllib.request, "urlopen",
        _urlopen_spy(calls, body=b'{"results": [], "next": null}'),
    )
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="secret-token-xyz")

    with caplog.at_level(logging.DEBUG, logger="casework.api"):
        api.get("/cases/")

    records = [r for r in caplog.records if r.name == "casework.api"]
    assert any(
        r.levelno == logging.DEBUG
        and "HTTP GET" in r.getMessage()
        and "-> 200" in r.getMessage()
        for r in records
    )


def test_patch_logs_at_info(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_spy(calls))
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="secret-token-xyz")

    with caplog.at_level(logging.DEBUG, logger="casework.api"):
        api.patch_field("some-slug", "bigo", 500)

    records = [r for r in caplog.records if r.name == "casework.api"]
    assert any(
        r.levelno == logging.INFO and "HTTP PATCH" in r.getMessage()
        for r in records
    )


def test_no_auth_material_ever_reaches_the_logs(monkeypatch, caplog):
    """Hard requirement: no Authorization header, bearer token, or Basic
    credentials may ever appear in a log line -- exercised across both auth
    modes and both a read and a write.
    """
    calls = []
    monkeypatch.setattr(
        urllib.request, "urlopen",
        _urlopen_spy(calls, body=b'{"results": [], "next": null}'),
    )

    token = "sekrit-bearer-token-should-never-leak"
    basic_user, basic_pass = "abgen", "sekrit-basic-password-should-never-leak"
    basic_creds = base64.b64encode(f"{basic_user}:{basic_pass}".encode()).decode()

    bearer_api = CaseworkApi(base_url="http://127.0.0.1:48010", token=token)
    basic_api = CaseworkApi(
        base_url="http://127.0.0.1:48010", basic=(basic_user, basic_pass)
    )

    with caplog.at_level(logging.DEBUG, logger="casework.api"):
        bearer_api.get("/cases/")
        bearer_api.patch_field("some-slug", "bigo", 500)
        basic_api.get("/cases/")
        basic_api.patch_field("some-slug", "bigo", 500)

    all_text = "\n".join(
        r.getMessage() for r in caplog.records if r.name == "casework.api"
    )
    assert token not in all_text
    assert basic_creds not in all_text
    assert "Bearer" not in all_text
    assert "Basic " not in all_text
    assert len(calls) == 4


def test_no_auth_material_reaches_the_logs_on_the_exception_path(monkeypatch, caplog):
    """Same hard requirement as above, but for the branch the previous test
    never touches: `_request`'s `except Exception as exc: ... logger.warning(
    ..., exc, ...)` line. `str(URLError)`/`str(HTTPError)` carries no auth or
    URL query by construction, but that was reasoning, not a test -- this
    exercises the exception path directly and greps every captured record.
    """
    token = "sekrit-bearer-token-should-never-leak-on-error"
    basic_user, basic_pass = "abgen", "sekrit-basic-password-should-never-leak-on-error"
    basic_creds = base64.b64encode(f"{basic_user}:{basic_pass}".encode()).decode()

    def raising_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", raising_urlopen)

    bearer_api = CaseworkApi(base_url="http://127.0.0.1:48010", token=token)
    basic_api = CaseworkApi(
        base_url="http://127.0.0.1:48010", basic=(basic_user, basic_pass)
    )

    with caplog.at_level(logging.DEBUG, logger="casework.api"):
        for api in (bearer_api, basic_api):
            with pytest.raises(urllib.error.URLError):
                api.get("/cases/")
            with pytest.raises(urllib.error.URLError):
                api.patch_field("some-slug", "bigo", 500)

    all_text = "\n".join(
        r.getMessage() for r in caplog.records if r.name == "casework.api"
    )
    assert all_text  # sanity: the exception path did log something
    assert token not in all_text
    assert basic_creds not in all_text
    assert "Bearer" not in all_text
    assert "Basic " not in all_text
