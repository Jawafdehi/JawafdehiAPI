"""HTTP client for the Jawafdehi control plane. No ORM, ever."""
import base64
import json
import logging
import time
import urllib.parse
import urllib.request

LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
# Methods that mutate server state. GET/HEAD (reads) are never guarded --
# only these go through the write-guard in `_request`.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Module logger for every HTTP call this client makes. NEVER log the
# Authorization header, the bearer token, or Basic credentials through this
# logger -- see `_request` below.
logger = logging.getLogger("casework.api")

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
# The case PATCH endpoint takes an RFC-6902 array. DRF registers no
# application/json-patch+json parser, so that content type 415s.
PATCH_CONTENT_TYPE = "application/json"
WHOLE_LIST_PATHS = ("evidence", "entities")


def build_replace_patch(field, value):
    return [{"op": "replace", "path": f"/{field}", "value": value}]


class CaseworkApi:
    """Control-plane HTTP client with two mutually exclusive auth modes.

    ``token`` -- production default. Sends ``Authorization: Bearer <token>``,
    decoded by ``jawafdehi_shared.auth.oidc.OIDCAuthentication`` in prod.

    ``basic`` -- local-dev only. Sends ``Authorization: Basic <user:pass>``.
    Only usable against a server run with ``DEV_AUTH=1`` (and ``DEBUG`` or
    ``TESTING``), which additively accepts DRF's ``BasicAuthentication`` --
    see ``config/settings.py:693-732``.

    Bearer stays first-class: ``OIDCAuthentication`` is always first in DRF's
    authenticator chain, so a request carrying a ``Bearer`` header is *always*
    routed to OIDC and never falls through to the local Basic/Session
    authenticators -- meaning Basic mode must send Basic, never Bearer, and
    vice versa. Exactly one of ``token``/``basic`` must be given so it is not
    possible to accidentally send both headers.
    """

    def __init__(self, base_url, token=None, *, basic=None, allow_remote_writes: bool = False):
        if not base_url:
            raise ValueError(
                "base_url is required: pass --api-base-url or set "
                "JAWAFDEHI_API_BASE (e.g. https://api.jawafdehi.org for "
                "production, http://127.0.0.1:48010 for a local DEV_AUTH server)"
            )
        self.base_url = base_url.rstrip("/")
        if not self.base_url.endswith("/api"):
            self.base_url += "/api"
        if (token is None) == (basic is None):
            raise ValueError(
                "CaseworkApi requires exactly one of `token` (Bearer, "
                "production default) or `basic=(username, password)` "
                "(HTTP Basic, local DEV_AUTH only) -- never both, never neither"
            )
        if basic is not None:
            host = urllib.parse.urlparse(self.base_url).hostname
            if host not in LOOPBACK_HOSTS:
                raise ValueError(
                    f"basic= is only permitted against loopback (127.0.0.1 or "
                    f"localhost); refusing to send Basic auth to {base_url!r} -- "
                    "use `token` (Bearer) for any non-local host"
                )
        self.token = token
        self.basic = basic
        # Write-guard opt-in. False (the default) means `_patch` refuses any
        # non-loopback `base_url` -- see `_patch` below. Reads are never
        # affected by this flag.
        self.allow_remote_writes = allow_remote_writes

    def _headers(self, content_type=None):
        if self.basic is not None:
            username, password = self.basic
            creds = base64.b64encode(f"{username}:{password}".encode()).decode()
            auth = f"Basic {creds}"
        else:
            auth = f"Bearer {self.token}"
        h = {"Authorization": auth, "User-Agent": BROWSER_UA,
             "Accept": "application/json"}
        if content_type:
            h["Content-Type"] = content_type
        return h

    def _request(self, method, url, data=None, headers=None, timeout=60):
        """The single method all HTTP goes through -- and the true write-guard
        choke point.

        Before anything else: if `method` is a write method (POST/PUT/PATCH/
        DELETE) AND the target host is not in `LOOPBACK_HOSTS` AND
        `allow_remote_writes` was not set, refuse -- raise `RuntimeError`
        BEFORE `urllib.request.urlopen` is ever called, so no request is
        attempted. GET/HEAD (reads) are never guarded. `_patch` carries its
        own copy of this same check (defense in depth; redundant once this
        one exists), but THIS is the check that also covers writes that don't
        go through `_patch` -- e.g. `convert.py`'s `upload_markdown`, which
        POSTs directly via `_request`.

        Logs method, PATH ONLY (never the full URL -- query strings can carry
        sensitive values) plus status and elapsed time. Reads (GET) log at
        DEBUG, writes (PATCH/POST/...) log at INFO. The `headers` dict (which
        carries the `Authorization` header) is NEVER passed to the logger --
        do not "helpfully" add it to a log line, even on the exception path.
        """
        split = urllib.parse.urlsplit(url)
        if (
            method in WRITE_METHODS
            and split.hostname not in LOOPBACK_HOSTS
            and not self.allow_remote_writes
        ):
            raise RuntimeError(
                f"refusing to write to non-loopback base_url {self.base_url!r} "
                f"(host={split.hostname!r}); pass allow_remote_writes=True to "
                "CaseworkApi (wired from the CLI via --allow-remote-writes) to "
                "opt in -- reads are unaffected by this guard"
            )
        path = split.path
        level = logging.DEBUG if method == "GET" else logging.INFO
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        start = time.monotonic()
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.warning(
                "HTTP %s %s -> ERROR %s (%dms)", method, path, exc, elapsed_ms
            )
            raise
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.log(level, "HTTP %s %s -> %s (%dms)", method, path, resp.status, elapsed_ms)
        return resp

    def get(self, path, params=None, timeout=60):
        url = self.base_url + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        with self._request("GET", url, headers=self._headers(), timeout=timeout) as r:
            return json.loads(r.read().decode())

    # The list endpoint's server-side default is 20 per page, so walking all
    # 3,003 cases costs 151 round trips -- ~2m45s against production, paid by
    # every enricher run before it processes a single case. The API caps
    # page_size at 200 (measured 2026-08-03: page_size=200/500/1000 all return
    # 200 results), so 200 is the largest useful ask and brings that to 16
    # requests. Callers can still override via `params`.
    PAGE_SIZE = 200

    def iter_cases(self, params=None, timeout=60):
        page, params = 1, dict(params or {})
        params.setdefault("page_size", self.PAGE_SIZE)
        while True:
            params["page"] = page
            data = self.get("/cases/", params, timeout)
            for case in data.get("results", []):
                yield case
            if not data.get("next"):
                return
            page += 1

    def get_case(self, slug, timeout=60):
        """Detail endpoint -- the ONLY one that resolves `material` on evidence."""
        return self.get("/cases/" + urllib.parse.quote(slug) + "/", timeout=timeout)

    def get_case_with_etag(self, slug, timeout=60):
        """Detail endpoint PLUS the response ETag, for optimistic-concurrency
        read-merge-write.

        Returns ``(body, etag)`` where ``etag`` is ``None`` when the server
        sends no ETag. The binder echoes ``etag`` back as ``If-Match`` on the
        PATCH so a concurrent edit landing between this read and that write is
        rejected with 412 (stale) instead of silently clobbering the other
        writer through the destructive whole-list replace (`replace_list`).
        Like every read, this is never write-guarded.
        """
        url = self.base_url + "/cases/" + urllib.parse.quote(slug) + "/"
        with self._request("GET", url, headers=self._headers(), timeout=timeout) as r:
            body = json.loads(r.read().decode())
            headers = getattr(r, "headers", None)
            etag = headers.get("ETag") if headers is not None else None
            return body, etag

    def _patch(self, slug, ops, timeout=60, if_match=None):
        """The choke point for FIELD writes (`patch_field`, `replace_list`) --
        NOT "every write": `convert.py`'s `upload_markdown` writes via a
        direct `_request("POST", ...)` call that never passes through here.

        The authoritative write-guard now lives in `_request` (see its
        docstring) and covers POST too, closing that gap. The check below is
        a redundant copy of the same guard, kept for defense in depth -- it
        refuses to fire a PATCH at any non-loopback host unless
        `allow_remote_writes=True` was passed to `__init__`. Reads (`get`,
        `iter_cases`, `get_case`) do NOT go through this method and are never
        guarded -- reads against production are allowed.
        """
        host = urllib.parse.urlparse(self.base_url).hostname
        if host not in LOOPBACK_HOSTS and not self.allow_remote_writes:
            raise RuntimeError(
                f"refusing to write to non-loopback base_url {self.base_url!r} "
                f"(host={host!r}); pass allow_remote_writes=True to CaseworkApi "
                "(wired from the CLI via --allow-remote-writes) to opt in -- "
                "reads are unaffected by this guard"
            )
        url = self.base_url + "/cases/" + urllib.parse.quote(slug) + "/"
        body = json.dumps(ops, ensure_ascii=False).encode("utf-8")
        headers = self._headers(PATCH_CONTENT_TYPE)
        # Optimistic concurrency: when the caller passes the ETag it read, echo
        # it as If-Match. Server enforcement is opt-in (it only checks If-Match
        # when present), so this MUST be sent for a stale read to be caught --
        # otherwise the whole-list replace overwrites whatever a concurrent
        # writer put there. Omitted (None) -> unconditional write, as before.
        if if_match:
            headers["If-Match"] = if_match
        with self._request("PATCH", url, data=body,
                           headers=headers, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else {}

    def patch_field(self, slug, field, value, timeout=60, if_match=None):
        return self._patch(slug, build_replace_patch(field, value), timeout, if_match=if_match)

    def replace_list(self, slug, path, items, timeout=60, if_match=None):
        """Whole-list replace for /evidence and /entities.

        DESTRUCTIVE: the server deletes every existing join row for this
        path and recreates from exactly the `items` given -- there is no
        partial/append mode. Passing a partial list silently DELETES the
        rows you omitted; there is no warning and no way to recover them
        from this call. Callers must GET the case, merge the full desired
        list in application code, and only then call replace_list with the
        FULL list -- never a delta.

        Pass `if_match` (the ETag from `get_case_with_etag`) to make the write
        conditional -- a 412 then means the case changed since you read it, so
        the merge is stale; re-read, re-merge, and retry rather than clobber.
        """
        if path not in WHOLE_LIST_PATHS:
            raise ValueError(f"{path} is not a whole-list path")
        return self._patch(slug, build_replace_patch(path, items), timeout, if_match=if_match)
