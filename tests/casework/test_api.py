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


def test_empty_base_url_raises():
    # No silent localhost default anywhere: an unset base_url (None or "") must
    # fail loud, not target some implicit host.
    with pytest.raises(ValueError, match="base_url is required"):
        CaseworkApi(base_url="", token="t")
    with pytest.raises(ValueError, match="base_url is required"):
        CaseworkApi(base_url=None, token="t")


def test_bearer_mode_rejects_cleartext_http_to_a_remote_host():
    """CWE-319. `_headers` attaches `Authorization: Bearer <token>` to every
    request, reads included, and the write-guard only inspects the host -- not
    the scheme. So an `http://` remote base URL put a production token on the
    wire in cleartext, and these runs are hours long.

    `basic=` was already loopback-only; Bearer had no scheme check at all.
    """
    with pytest.raises(ValueError, match="https"):
        CaseworkApi(base_url="http://api.jawafdehi.org", token="prod-token")


def test_bearer_mode_accepts_https_to_a_remote_host():
    CaseworkApi(base_url="https://api.jawafdehi.org", token="prod-token")


@pytest.mark.parametrize("base_url", [
    "http://127.0.0.1:48010",
    "http://localhost:48010",
])
def test_bearer_mode_still_allows_cleartext_to_loopback(base_url):
    """A local DEV_AUTH server has no TLS, and a token never leaves the host.
    Requiring https here would break every local run for no gain."""
    CaseworkApi(base_url=base_url, token="local-token")


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
# get_case_with_etag + If-Match -- optimistic concurrency for the binder's
# read-merge-write. The ETag from the detail GET is echoed back as If-Match on
# the PATCH, so a concurrent edit landing between the read and the write yields
# 412 (stale) instead of silently clobbering the other writer through the
# destructive whole-list replace.
# ---------------------------------------------------------------------------


class _RespWithHeaders:
    def __init__(self, body=b"{}", status=200, headers=None):
        self.status = status
        self._body = body
        self.headers = headers if headers is not None else {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_get_case_with_etag_returns_body_and_etag(monkeypatch):
    seen = {}

    def fake_request(method, url, data=None, headers=None, timeout=None):
        seen.update(method=method, url=url)
        return _RespWithHeaders(
            body=b'{"slug": "x", "state": "DRAFT"}',
            headers={"ETag": 'W/"abc123"'},
        )

    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "_request", fake_request)

    body, etag = api.get_case_with_etag("a slug?x")

    assert seen["method"] == "GET"
    assert seen["url"] == "http://127.0.0.1:48010/api/cases/a%20slug%3Fx/"
    assert body["state"] == "DRAFT"
    assert etag == 'W/"abc123"'


def test_get_case_with_etag_tolerates_missing_etag(monkeypatch):
    def fake_request(method, url, data=None, headers=None, timeout=None):
        return _RespWithHeaders(body=b'{"slug": "x"}', headers={})

    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "_request", fake_request)

    body, etag = api.get_case_with_etag("x")

    assert body["slug"] == "x"
    assert etag is None


def test_replace_list_sends_if_match_header_when_given(monkeypatch):
    seen = {}
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "_request", _fake_request_capturing(seen))

    api.replace_list("some-slug", "evidence", [], if_match='W/"etag-1"')

    assert seen["headers"]["If-Match"] == 'W/"etag-1"'


def test_replace_list_omits_if_match_header_by_default(monkeypatch):
    seen = {}
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "_request", _fake_request_capturing(seen))

    api.replace_list("some-slug", "evidence", [])

    assert "If-Match" not in seen["headers"]


def test_patch_field_sends_if_match_header_when_given(monkeypatch):
    seen = {}
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "_request", _fake_request_capturing(seen))

    api.patch_field("some-slug", "bigo", 500, if_match='W/"etag-9"')

    assert seen["headers"]["If-Match"] == 'W/"etag-9"'


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


@pytest.mark.parametrize("params,expected", [
    # Without an explicit page_size the server pages at its default 20, so a full
    # list costs 151 round trips (~2m45s against production) before any enricher
    # does a single case of work. The API caps page_size at 200 (measured:
    # 200/500/1000 all return 200), so 200 is the most a client can get and cuts
    # that to 16 requests -- verified end to end at 16 requests / 33.7s.
    (None, 200),
    # ...and the caller can still override it.
    ({"page_size": 5}, 5),
])
def test_iter_cases_page_size(monkeypatch, params, expected):
    seen = []

    def fake_get(path, params=None, timeout=60):
        seen.append(dict(params or {}))
        return {"results": [], "next": None}

    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "get", fake_get)

    list(api.iter_cases(params))

    assert seen[0]["page_size"] == expected


# ---------------------------------------------------------------------------
# Progress. Listing the 3,003-case production list is 16 sequential requests
# and ~33s during which NOTHING was logged, so a run was indistinguishable
# from a hang -- an operator has no way to tell whether to wait or Ctrl-C.
# ---------------------------------------------------------------------------


def _paged(n_pages, per_page=2):
    """`n_pages` DRF-shaped pages, each with `per_page` results and a `count`."""
    total = n_pages * per_page
    return {
        page: {
            "results": [{"slug": f"case-{page}-{i}"} for i in range(per_page)],
            "count": total,
            "next": f"http://x/?page={page + 1}" if page < n_pages else None,
        }
        for page in range(1, n_pages + 1)
    }


def test_iter_cases_reports_progress_after_every_page(monkeypatch):
    """One callback per page, carrying enough to render a live counter: which
    page, how many cases so far, and the server's total."""
    pages = _paged(3)
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "get", lambda path, params=None, timeout=60: pages[params["page"]])

    seen = []
    list(api.iter_cases(progress=lambda **kw: seen.append(kw)))

    assert seen == [
        {"page": 1, "fetched": 2, "total": 6},
        {"page": 2, "fetched": 4, "total": 6},
        {"page": 3, "fetched": 6, "total": 6},
    ]


def test_iter_cases_progress_tolerates_a_server_that_sends_no_count(monkeypatch):
    """`total` is whatever the server said, and DRF only sends `count` when the
    paginator is countable. Reporting must degrade, not raise."""
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(
        api, "get",
        lambda path, params=None, timeout=60: {"results": [{"slug": "a"}], "next": None},
    )

    seen = []
    list(api.iter_cases(progress=lambda **kw: seen.append(kw)))

    assert seen == [{"page": 1, "fetched": 1, "total": None}]


def test_iter_cases_without_a_callback_still_logs_progress(monkeypatch, caplog):
    """The default has to be useful on its own. Five of the six enrichers do not
    wire a callback, and they have the same 33s of silence to explain.
    """
    pages = _paged(2)
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "get", lambda path, params=None, timeout=60: pages[params["page"]])

    with caplog.at_level(logging.INFO, logger="casework.api"):
        list(api.iter_cases())

    progress = [r.getMessage() for r in caplog.records if "page" in r.getMessage()]
    assert len(progress) == 2, progress
    assert "1" in progress[0] and "4" in progress[0], progress[0]


def test_iter_cases_progress_is_emitted_before_the_next_request(monkeypatch):
    """Reporting after the whole loop would be useless -- the point is feedback
    DURING the wait. Each page's callback must fire before the next GET goes out.
    """
    pages = _paged(3)
    order = []
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")

    def fake_get(path, params=None, timeout=60):
        order.append(f"get{params['page']}")
        return pages[params["page"]]

    monkeypatch.setattr(api, "get", fake_get)
    list(api.iter_cases(progress=lambda **kw: order.append(f"progress{kw['page']}")))

    assert order == [
        "get1", "progress1", "get2", "progress2", "get3", "progress3",
    ]


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


# ---------------------------------------------------------------------------
# search_entities -- candidate retrieval for the resolver, over the unified
# search endpoint (`/api/search/`, OpenSearch-backed) rather than
# `/api/entities?query=` (which only scores the first 5000 IRI-ordered rows
# and has ~3% recall against prod's 162,650 `person` entities). Pages while
# the last page's lowest score still ties the first page's top score, so a
# block of identical-name entities spanning a page boundary is never
# truncated mid-tie -- that would hide a duplicate from the resolver's
# ambiguity veto and turn a review into a silent bind.
# ---------------------------------------------------------------------------


def test_search_entities_hits_the_unified_search_endpoint(monkeypatch):
    api = CaseworkApi("http://127.0.0.1:48010", token="t")
    seen = []

    def fake_get(path, params=None, timeout=60):
        seen.append((path, params))
        return {"results": [], "count": 0}

    monkeypatch.setattr(api, "get", fake_get)
    api.search_entities("अनिष श्रेष्ठ")

    path, params = seen[0]
    assert path == "/search/"
    assert params["q"] == "अनिष श्रेष्ठ"
    assert params["type"] == "entity"


def test_search_entities_pages_on_to_exhaust_a_top_score_tie(monkeypatch):
    # A block of identical-name entities must never be truncated mid-tie: a
    # hidden duplicate would let the resolver's ambiguity veto pass and produce
    # a bind. Pages 1 and 2 are full pages entirely at the top score, so paging
    # must continue; page 3 drops below it, which ends the walk.
    pages = [
        {"results": [{"id": f"https://jawafdehi.org/entity/person/p{i}",
                      "score": 182.17} for i in range(3)]},
        {"results": [{"id": f"https://jawafdehi.org/entity/person/q{i}",
                      "score": 182.17} for i in range(3)]},
        {"results": [{"id": "https://jawafdehi.org/entity/person/r0", "score": 12.0}]},
    ]
    calls = []

    api = CaseworkApi("http://127.0.0.1:48010", token="t")

    def fake_get(path, params=None, timeout=60):
        calls.append(params["page"])
        return pages[params["page"] - 1]

    monkeypatch.setattr(api, "get", fake_get)
    results = api.search_entities("मदन यादव", page_size=3)

    assert calls == [1, 2, 3]          # kept going while the tie held
    assert len(results) == 7


def test_search_entities_stops_on_a_short_page(monkeypatch):
    # Fewer results than page_size means the result set is exhausted -- there
    # is no page 2 worth asking for, even while the top-score tie still holds.
    calls = []
    api = CaseworkApi("http://127.0.0.1:48010", token="t")

    def fake_get(path, params=None, timeout=60):
        calls.append(params["page"])
        return {"results": [{"id": "https://jawafdehi.org/entity/person/p0",
                             "score": 182.17}]}

    monkeypatch.setattr(api, "get", fake_get)
    api.search_entities("मदन यादव", page_size=3)
    assert calls == [1]


def test_search_entities_stops_at_the_page_cap(monkeypatch):
    api = CaseworkApi("http://127.0.0.1:48010", token="t")
    calls = []

    def fake_get(path, params=None, timeout=60):
        calls.append(params["page"])
        return {"results": [{"id": f"https://jawafdehi.org/entity/person/p{params['page']}",
                             "score": 100.0}]}

    monkeypatch.setattr(api, "get", fake_get)
    api.search_entities("थापा", page_size=1, pages=2)
    assert calls == [1, 2]


def test_search_entities_is_a_read_so_the_write_guard_never_fires(monkeypatch):
    # Non-loopback host, allow_remote_writes unset: a read must still work.
    api = CaseworkApi("https://api.jawafdehi.org", token="t")
    monkeypatch.setattr(api, "get", lambda path, params=None, timeout=60: {"results": []})
    assert api.search_entities("अनिष श्रेष्ठ") == []
