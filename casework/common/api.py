"""HTTP client for the Jawafdehi control plane. No ORM, ever."""
import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


class EntityAlreadyExists(Exception):
    """A create POST hit an entity that is already there.

    Its own type because the caller's response is usually to BIND the existing
    entity, not to record an error: someone -- a caseworker, or an earlier run
    over the same case -- got there first, which is the outcome we wanted. NOT
    universally, though: a caller that reached the create BECAUSE it could not
    identify the entity by name must refuse instead, since the collision says
    only that the slug is taken, not that it is taken by this person. See
    `casework.enrich_court_record.resolve_defendant`. The server
    answers 409 `ENTITY_EXISTS`: `_map_service_value_error` maps the duplicate
    `@id` check (`entities/services/publication/service.py:68`) to 409, and
    reserves 422 for `validate_create_payload` failures (`entities/views.py:220,
    420`). Keyed on the status alone -- an earlier version keyed on 422, and
    every re-run then recorded `error` and rebound nothing.
    """


class CandidateList(list):
    """Search results, plus whether they are the COMPLETE result set.

    A plain list cannot say why paging stopped, and the two reasons matter very
    differently to the resolver:

    * a short page means the results ran out, so the list is everything there is;
    * stopping early on relevance, or hitting the page cap, means more rows exist
      and this window may have cut through a block of same-name entities -- which
      is exactly the premise the ambiguity veto needs.

    `search_entities` used to compute this distinction in its for/else and then
    throw it away, leaving the resolver to guess from `len(candidates)`. A list
    subclass carries it without breaking any caller that just iterates or takes
    a length.

    Defaults to `complete = False`: a bare `CandidateList()` claims nothing, so
    a caller that forgets to set it gets the cautious answer rather than a silent
    assurance of completeness.
    """

    complete: bool = False
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
# Candidate-retrieval window for `search_entities`. 50 is the endpoint's
# MAX_PAGE_SIZE (`search/service.py`); 4 pages = 200 candidates is well past the
# largest same-name block seen in prod (13, for "संजय प्रसाद यादव").
ENTITY_SEARCH_PAGE_SIZE = 50
ENTITY_SEARCH_MAX_PAGES = 4


def build_replace_patch(field, value):
    return [{"op": "replace", "path": f"/{field}", "value": value}]


def build_replace_ops(pairs):
    """One RFC-6902 document replacing SEVERAL scalar fields at once.

    `pairs` is an iterable of `(field, value)`. Empty in -> empty out, so a
    caller whose fields all failed validation sends nothing rather than an empty
    patch the server would have to reason about.

    WHY THIS EXISTS. `patch_field` is one request per field, and each request
    changes the case's ETag -- so a caller writing two fields under the ETag it
    read at the top gets a 412 on the second write, every time. That is not
    hypothetical: `enrich_card` shipped that way and could never write both
    `title` and `short_description` in one pass (2026-08-04 smoke run). Sending
    both ops in ONE conditional request removes the failure instead of handling
    it -- the server applies the whole array against a single snapshot, so the
    write is atomic and the `If-Match` covers exactly the state that was read.
    """
    return [
        {"op": "replace", "path": f"/{field}", "value": value}
        for field, value in pairs
    ]


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
        parsed = urllib.parse.urlparse(self.base_url)
        is_loopback = parsed.hostname in LOOPBACK_HOSTS
        if basic is not None and not is_loopback:
            raise ValueError(
                f"basic= is only permitted against loopback (127.0.0.1 or "
                f"localhost); refusing to send Basic auth to {base_url!r} -- "
                "use `token` (Bearer) for any non-local host"
            )
        # `_headers` attaches the credential to EVERY request, reads included,
        # and the write-guard below only inspects the host -- not the scheme. An
        # `http://` remote base URL therefore put a production token on the wire
        # in cleartext (CWE-319), for runs that last hours. Loopback stays
        # exempt: a local DEV_AUTH server has no TLS and the token never leaves
        # the host, so requiring https there would break every local run for no
        # gain.
        if not is_loopback and parsed.scheme != "https":
            raise ValueError(
                f"refusing to send credentials to {base_url!r} over "
                f"{parsed.scheme or 'no'} -- a remote base_url must use https, "
                "or the Authorization header travels in cleartext. Use "
                "http://127.0.0.1:48010 for a local DEV_AUTH server."
            )
        self.token = token
        self.basic = basic
        # Write-guard opt-in. False (the default) means `_patch` refuses any
        # non-loopback `base_url` -- see `_patch` below. Reads are never
        # affected by this flag.
        self.allow_remote_writes = allow_remote_writes
        # Run-scoped read caches for the two entity reads. Keyed on the exact
        # argument, so a hit returns what a second request would have returned:
        # NES is fixed for a run's duration and both endpoints are read-only.
        # These exist because corruption cases name the SAME institutions over
        # and over -- a ministry or a district office recurs across many cases in
        # one batch, and each recurrence otherwise re-pays a paged search (up to
        # four round trips) or a document GET for an answer already in hand.
        # Scoped to the instance, not the class, so the cache dies with the run
        # and can never serve a stale document to a later one.
        self._entity_search_cache: dict[tuple, list] = {}
        self._entity_doc_cache: dict[str, dict] = {}

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

    def entity_prefixes(self, timeout=60):
        """Every entity prefix currently in use, from `GET /api/entity_prefixes`.

        A READ, so it is safe against production and unaffected by the write
        guard. Not a whitelist: the endpoint answers `SELECT DISTINCT prefix`
        over live entities (`entities/persistence.py:313`), so it reports what
        exists rather than what is permitted -- which is exactly why
        `casework.entity_identity.prefix_is_creatable` has to decide, and why an
        unchecked prefix would ratify itself by appearing here on the next call.
        """
        return list(self.get("/entity_prefixes", timeout=timeout).get("prefixes") or ())

    def create_entity(self, payload, timeout=60):
        """POST one NES entity. Returns the created document (with its `@id`).

        Raises `EntityAlreadyExists` when the IRI is taken, so the caller can
        bind the existing entity instead of failing the case.

        Goes through `_request` rather than `self.get`/`_patch`, the same way
        `convert.py`'s `upload_markdown` does, which is what puts it under the
        host write-guard: a non-loopback POST is refused unless
        `allow_remote_writes` is set. No second guard to keep in step.

        The payload is the API's authoring form -- `prefix`, `slug`, `type`,
        `name` plus any schema.org properties, which
        `normalize_authoring_payload` copies through verbatim
        (`entities/write_validation.py:113`). It builds the `@id` from
        prefix+slug itself, so we never send one.
        """
        url = self.base_url + "/entities"
        body = json.dumps(payload).encode("utf-8")
        headers = dict(self._headers())
        headers["Content-Type"] = "application/json"
        try:
            with self._request("POST", url, data=body, headers=headers,
                               timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            # A duplicate IRI is 409 ENTITY_EXISTS, NOT 422. The view's 422 is for
            # validation (`entities/views.py:219`); the duplicate check raises a
            # ValueError that `_map_service_value_error` turns into 409
            # (`entities/views.py:420`). Verified against the local harness --
            # keying this off 422 made every re-run record `error` and rebind
            # nothing, which no stubbed test caught because the stub chose the
            # status.
            if exc.code == 409:
                raise EntityAlreadyExists(
                    exc.read().decode(errors="replace")) from exc
            if exc.code == 422:
                raise ValueError(
                    "entity create rejected: "
                    f"{exc.read().decode(errors='replace')}") from exc
            raise

    # The list endpoint's server-side default is 20 per page, so walking all
    # 3,003 cases costs 151 round trips -- ~2m45s against production, paid by
    # every enricher run before it processes a single case. The API caps
    # page_size at 200 (measured 2026-08-03: page_size=200/500/1000 all return
    # 200 results), so 200 is the largest useful ask and brings that to 16
    # requests. Callers can still override via `params`.
    PAGE_SIZE = 200

    def iter_cases(self, params=None, timeout=60, progress=None):
        """Yield every case, following pagination.

        Even at `PAGE_SIZE`, listing production is 16 sequential requests and
        ~33s, and it runs before an enricher does a single case of work. With no
        output during it a run is indistinguishable from a hang, so each page
        reports `(page, fetched, total)` as soon as it lands -- during the wait,
        not after it.

        `progress` is called with those as keywords. Pass one to route the
        report into a run's own logger and events file; the default logs to
        `casework.api`, so an enricher that wires nothing still narrates.
        """
        page, params = 1, dict(params or {})
        params.setdefault("page_size", self.PAGE_SIZE)
        progress = progress or self._log_list_progress
        fetched = 0
        while True:
            params["page"] = page
            data = self.get("/cases/", params, timeout)
            results = data.get("results", [])
            fetched += len(results)
            # Report before yielding: a consumer that stops early still gets a
            # record of the work already paid for.
            progress(page=page, fetched=fetched, total=data.get("count"))
            yield from results
            if not data.get("next"):
                return
            page += 1

    @staticmethod
    def _log_list_progress(page, fetched, total):
        """Default `iter_cases` reporter. `total` is absent on an uncountable
        paginator, so the denominator degrades to '?' rather than raising."""
        logger.info(
            "case list: page %s, %s/%s fetched", page, fetched,
            total if total is not None else "?",
        )

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

    def get_court_case_entities(self, court, number, timeout=60):
        """Every party row on one NGM court case, following pagination.

        Rows carry `side` ("plaintiff" | "defendant"), `name`, and an `nes_id`
        that is usually null -- see `casework.court_record` for why the accused
        path reads the names and resolves them rather than trusting that field.

        Pages by page NUMBER and ignores the response's `next` URL: `get()`
        builds its request as `base_url + path`, so an absolute `next` would be
        concatenated onto the base and produce a doubled prefix. `iter_cases`
        sets the same precedent.
        """
        path = (f"/courtcases/{urllib.parse.quote(str(court), safe='')}"
                f"/{urllib.parse.quote(str(number), safe='')}/entities")
        rows, page = [], 1
        while True:
            data = self.get(path, {"page_size": 200, "page": page}, timeout)
            rows.extend(data.get("results") or [])
            if not data.get("next"):
                return rows
            page += 1

    def get_courtcase(self, court, number, timeout=60):
        """One NGM court case: registration date, case_status, parties summary.

        The composite-key detail route, keyed on (court, case_number). Public on
        the read plane, so this works with no credentials at all.
        """
        path = (f"/courtcases/{urllib.parse.quote(str(court), safe='')}"
                f"/{urllib.parse.quote(str(number), safe='')}/")
        return self.get(path, timeout=timeout)

    def list_hearings(self, court, number, timeout=60):
        """Every hearing row on one court case, following pagination.

        Rows are NOT returned in date order -- the deciding hearing can sort
        before an earlier one. Callers pick by max `hearing_date_ad`, never by
        list position.

        Pages by page NUMBER and ignores the response's `next` URL, for the same
        reason `get_court_case_entities` does: `get()` concatenates path onto
        base_url, so an absolute `next` would produce a doubled prefix.
        """
        path = (f"/courtcases/{urllib.parse.quote(str(court), safe='')}"
                f"/{urllib.parse.quote(str(number), safe='')}/hearings")
        rows, page = [], 1
        while True:
            data = self.get(path, {"page": page, "page_size": 100}, timeout=timeout)
            batch = data.get("results") or []
            rows.extend(batch)
            if len(batch) < 100:
                return rows
            page += 1

    def patch_case(self, slug, *, fields=(), lists=(), timeout=60, if_match=None):
        """Write scalar fields AND whole-list paths in ONE conditional request.

        `fields` is `[(name, value)]` of scalars; `lists` is `[(path, items)]`
        of whole-list paths (`WHOLE_LIST_PATHS`).

        WHY THIS EXISTS. `patch_fields` refuses whole-list paths and
        `replace_list` takes one path at a time, so a caller writing
        `case_start_date` and `entities` had to send two requests -- and the
        first changes the ETag, so the second 412s under the ETag read at the
        top. `build_replace_ops` records the same failure from `enrich_card`.

        THE MERGE-FIRST CONTRACT OF `replace_list` APPLIES IN FULL to every
        entry in `lists`: the server deletes every existing join row for that
        path and recreates from exactly the items given. Pass the FULL merged
        list, never a delta. Omitting a row deletes it, with no warning and no
        recovery.

        Empty in -> no request and `{}` out, so a case with nothing to change
        costs no write.
        """
        fields, lists = list(fields), list(lists)
        for name, _ in fields:
            if name in WHOLE_LIST_PATHS:
                raise ValueError(
                    f"{name} is a whole-list path -- pass it in `lists`, which "
                    "carries the merge-first contract")
        for path, _ in lists:
            if path not in WHOLE_LIST_PATHS:
                raise ValueError(f"{path} is not a whole-list path")
        ops = build_replace_ops(fields) + build_replace_ops(lists)
        if not ops:
            return {}
        return self._patch(slug, ops, timeout, if_match=if_match)

    def search_entities(self, query, *, page_size=ENTITY_SEARCH_PAGE_SIZE,
                        pages=ENTITY_SEARCH_MAX_PAGES, timeout=60):
        """Candidate NES entities for `query`, from the unified search endpoint.

        Uses `/api/search/` (OpenSearch) and NOT `/api/entities?query=`: that
        endpoint scores a textual query over only the first 5000 rows ordered by
        IRI (`MAX_SEARCH_CANDIDATES` in `entities/persistence.py`), and prod NES
        holds 162,650 `person` entities -- every result comes from the "a..."
        slice, so recall is about 3% and an ambiguity check built on it would be
        meaningless.

        Keeps paging while the last page's LOWEST score still ties the first
        page's top score. A block of identical-name entities truncated mid-tie
        would hide a duplicate from the resolver's ambiguity veto and turn a
        review into a bind. Capped at `pages` pages; the cap being reached is
        logged, never silent.

        A read, so the write-guard in `_request` never applies -- this is usable
        against production.

        Memoised per run (see `__init__`): a repeated query returns the first
        answer instead of re-paging. Keyed on the paging arguments too, so a
        caller asking for a different window is never served the wrong one.
        """
        cache_key = (query, page_size, pages)
        if cache_key in self._entity_search_cache:
            return self._entity_search_cache[cache_key]
        results, top_score, complete = CandidateList(), None, False
        for page in range(1, pages + 1):
            data = self.get("/search/", {"q": query, "type": "entity",
                                         "page_size": page_size, "page": page},
                            timeout=timeout)
            batch = data.get("results") or []
            results.extend(batch)
            if not batch:
                complete = True          # no more rows exist
                break
            scores = [r.get("score") or 0.0 for r in batch]
            if top_score is None:
                top_score = max(scores)
            if len(batch) < page_size:
                complete = True          # a short page IS the end of the results
                break
            if min(scores) < top_score:
                # Stopped early on relevance, on a FULL page -- so more rows do
                # exist and this window may have cut through a block of
                # same-name entities. NOT complete: the resolver decides what
                # that costs, using the score at the window edge.
                break
        else:
            logger.info(
                "entity search for %r hit the %d-page cap (%d candidates); a "
                "same-name tie may extend past it", query, pages, len(results))
        results.complete = complete
        self._entity_search_cache[cache_key] = results
        return results

    def get_entity(self, ref, timeout=60):
        """One NES entity document, by canonical IRI or bare ``<prefix>/<slug>``.

        `/api/search/` returns only `id`, `title` and `score`, so it cannot tell
        an Election Commission 2079 candidate record apart from a person NES
        holds because a CIAA case named them. This is the read that can:
        `identifier` carries the `ecn-candidate-id` marker that
        `casework.entity_resolver.is_election_candidate_record` vetoes on.

        The detail route (`entities/urls.py`'s `_REF`) takes either form, but
        they need DIFFERENT encoding and the difference is not cosmetic --
        both spellings below were checked against prod:

        * `person/khusilala-saha-865cdc` keeps its separator, so quote with
          ``safe="/"``.
        * A full IRI must be encoded WHOLE (``safe=""``). Leaving the
          ``https://`` slashes bare puts a ``//`` in the request path, which
          collapses in transit and 404s.

        A read, so it goes through `self.get` and the write-guard in `_request`
        never applies -- usable against production.

        Memoised per run (see `__init__`): the same entity bound on several cases
        in one batch is fetched once. The document is read-only, so a hit is
        indistinguishable from a second request.
        """
        ref = (ref or "").strip()
        if not ref:
            raise ValueError("get_entity needs an entity IRI or a <prefix>/<slug> path")
        if ref in self._entity_doc_cache:
            return self._entity_doc_cache[ref]
        is_iri = ref.startswith("http://") or ref.startswith("https://")
        quoted = urllib.parse.quote(ref, safe="" if is_iri else "/")
        document = self.get("/entities/" + quoted, timeout=timeout)
        self._entity_doc_cache[ref] = document
        return document

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

    def patch_fields(self, slug, pairs, timeout=60, if_match=None):
        """Write several scalar fields in ONE conditional request.

        Prefer this over a `patch_field` loop whenever a caller writes more than
        one field under a single ETag -- see `build_replace_ops` for why a loop
        cannot work. Returns the server's response body, or `{}` when `pairs` is
        empty (no request is made).
        """
        pairs = list(pairs)
        # The whole-list paths are routed to `replace_list` on purpose: they are
        # DESTRUCTIVE replaces that require the caller to have merged the full
        # list first, and that contract is documented there, not here. Without
        # this check, `patch_fields(slug, [("evidence", [...])])` would perform
        # the same destructive replace while skipping the guard.
        for field, _ in pairs:
            if field in WHOLE_LIST_PATHS:
                raise ValueError(
                    f"{field} is a whole-list path -- use replace_list, which "
                    "documents the merge-first contract")
        ops = build_replace_ops(pairs)
        if not ops:
            return {}
        return self._patch(slug, ops, timeout, if_match=if_match)

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
