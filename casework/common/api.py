"""HTTP client for the Jawafdehi control plane. No ORM, ever."""
import base64
import json
import urllib.parse
import urllib.request

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
    see ``config/settings.py:693-732`` and ``casework/ab/README.md``.

    Bearer stays first-class: ``OIDCAuthentication`` is always first in DRF's
    authenticator chain, so a request carrying a ``Bearer`` header is *always*
    routed to OIDC and never falls through to the local Basic/Session
    authenticators -- meaning Basic mode must send Basic, never Bearer, and
    vice versa. Exactly one of ``token``/``basic`` must be given so it is not
    possible to accidentally send both headers.
    """

    def __init__(self, base_url, token=None, *, basic=None):
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
            if host not in ("127.0.0.1", "localhost"):
                raise ValueError(
                    f"basic= is only permitted against loopback (127.0.0.1 or "
                    f"localhost); refusing to send Basic auth to {base_url!r} -- "
                    "use `token` (Bearer) for any non-local host"
                )
        self.token = token
        self.basic = basic

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
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        return urllib.request.urlopen(req, timeout=timeout)

    def get(self, path, params=None, timeout=60):
        url = self.base_url + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        with self._request("GET", url, headers=self._headers(), timeout=timeout) as r:
            return json.loads(r.read().decode())

    def iter_cases(self, params=None, timeout=60):
        page, params = 1, dict(params or {})
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

    def _patch(self, slug, ops, timeout=60):
        url = self.base_url + "/cases/" + urllib.parse.quote(slug) + "/"
        body = json.dumps(ops, ensure_ascii=False).encode("utf-8")
        with self._request("PATCH", url, data=body,
                           headers=self._headers(PATCH_CONTENT_TYPE), timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else {}

    def patch_field(self, slug, field, value, timeout=60):
        return self._patch(slug, build_replace_patch(field, value), timeout)

    def replace_list(self, slug, path, items, timeout=60):
        """Whole-list replace for /evidence and /entities.

        DESTRUCTIVE: the server deletes every existing join row for this
        path and recreates from exactly the `items` given -- there is no
        partial/append mode. Passing a partial list silently DELETES the
        rows you omitted; there is no warning and no way to recover them
        from this call. Callers must GET the case, merge the full desired
        list in application code, and only then call replace_list with the
        FULL list -- never a delta.
        """
        if path not in WHOLE_LIST_PATHS:
            raise ValueError(f"{path} is not a whole-list path")
        return self._patch(slug, build_replace_patch(path, items), timeout)
