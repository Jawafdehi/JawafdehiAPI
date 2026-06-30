"""Public discovery views: robots.txt + sitemap index/section + ResourceSync.

This module owns ALL of the public discovery surfaces:

* ``robots.txt`` — points crawlers at the sitemap + ResourceSync.
* the sitemap INDEX + child-sitemap (section) views — thin wrappers over
  ``django.contrib.sitemaps`` that (a) emit child-sitemap ``<loc>`` links on the
  canonical :func:`iri_base` host (NOT the request Host, a cache-poisoning
  vector) and (b) add a server-side cache.
* the three ResourceSync documents.

All of these are public, unauthenticated, cacheable GETs.

CACHING
-------
``cache_control`` is only a *client* hint; it does not stop the server from
re-scanning the corpus on every request. The hot paths (sitemap index, child
sitemaps, ResourceSync resource list) are wrapped with a low-level server-side
cache keyed on a cheap corpus-version stamp (``max_lastmod`` + ``count``) so a
second request reuses the rendered document instead of re-materializing ~8 full
table scans. The stamp makes the cache self-invalidating on any data change.
Under ``TESTING`` the cache is bypassed so tests observe fresh scans / call
counts deterministically.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.sitemaps.views import index as _django_sitemap_index
from django.contrib.sitemaps.views import sitemap as _django_sitemap_section
from django.core.cache import cache
from django.http import HttpResponse
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

from . import corpus, resourcesync
from .sitemaps import SITEMAPS

_XML = "application/xml"

#: Server-side cache TTL for the discovery documents (seconds). The corpus
#: version stamp in the key means stale data is busted before this anyway; the
#: TTL just bounds memory growth and re-stamps periodically.
DISCOVERY_CACHE_TTL = 3600


def _xml_response(body: str) -> HttpResponse:
    return HttpResponse(body, content_type=_XML)


def _corpus_version() -> str:
    """A cheap stamp that changes whenever the public corpus changes.

    Combines ``MAX(updated_at)`` and the row count across all types (both DB
    aggregates, no row materialization). Used to key the discovery caches so a
    data change busts them without a flush.
    """
    return f"{corpus.max_lastmod()}|{corpus.count_resources()}"


def _cached_document(cache_key_prefix: str, request, render):
    """Return ``render()``'s response, served from a server-side cache.

    Keyed on the request's full path + the corpus version stamp. Bypassed under
    ``TESTING`` so tests see fresh renders / deterministic call counts.

    ``render`` must return an ``HttpResponse`` (or ``TemplateResponse``); only
    the rendered bytes + content-type are cached.
    """
    if getattr(settings, "TESTING", False) and not getattr(
        settings, "DISCOVERY_CACHE_IN_TESTS", False
    ):
        resp = render()
        if hasattr(resp, "render"):
            resp.render()
        return resp

    import hashlib

    raw = f"{cache_key_prefix}:{request.get_full_path()}:{_corpus_version()}"
    # Hash so the key is backend-safe (no spaces/colons that break memcached).
    key = f"discovery:{hashlib.sha256(raw.encode()).hexdigest()}"
    cached = cache.get(key)
    if cached is not None:
        content, content_type = cached
        return HttpResponse(content, content_type=content_type)

    resp = render()
    if hasattr(resp, "render"):
        resp.render()  # TemplateResponse: realize the bytes before caching.
    cache.set(key, (resp.content, resp.headers.get("Content-Type", _XML)), DISCOVERY_CACHE_TTL)
    return resp


class _CanonicalSite:
    """A minimal Site-like object pinning the canonical IRI authority.

    Django's sitemap ``index`` view builds child-sitemap ``<loc>`` URLs as
    ``{request.scheme}://{site.domain}{path}``. With ``django.contrib.sites``
    intentionally NOT installed, the default falls back to ``RequestSite`` (the
    request Host header) — a host-header / cache-poisoning vector and
    inconsistent with the canonical authority that the child ``<loc>`` values
    inside ALREADY use. We pass this object as the ``site`` so the index emits
    the canonical host instead. (``get_current_site`` honours an explicit
    ``site`` only via the section view; the index view reads it itself, so we
    inject it through the request — see ``_canonical_request`` below.)
    """

    def __init__(self, domain: str):
        self.domain = domain
        self.name = domain


def _canonical_host_and_scheme() -> tuple[str, str]:
    from urllib.parse import urlsplit

    from jawafdehi_shared.entities.ids import iri_base

    parts = urlsplit(iri_base())
    return parts.netloc, (parts.scheme or "https")


# ── robots.txt ───────────────────────────────────────────────────────────────


@require_GET
@cache_control(public=True, max_age=3600)
def robots_txt(request) -> HttpResponse:
    """robots.txt — points crawlers at the new IRI-driven sitemap + ResourceSync.

    Built absolute from the canonical IRI base so the advertised URLs always carry
    the platform authority (matching the IRIs themselves), independent of the
    request host.
    """
    from jawafdehi_shared.entities.ids import iri_base

    base = iri_base().rstrip("/")
    lines = [
        "User-agent: *",
        "Allow: /",
        f"Sitemap: {base}/sitemap.xml",
        # ResourceSync entry point (non-standard in robots.txt but harmless;
        # the canonical discovery path is /.well-known/resourcesync).
        f"# ResourceSync: {base}{resourcesync.WELL_KNOWN_PATH}",
        "",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


# ── Sitemap index + section (canonical host + server cache) ──────────────────


@require_GET
@cache_control(public=True, max_age=3600)
def sitemap_index(request) -> HttpResponse:
    """Sitemap INDEX, emitting child-sitemap links on the canonical IRI host.

    Wraps Django's ``index`` view but forces the child ``<sitemap><loc>`` host to
    the canonical authority (``iri_base()``) instead of the request Host. We do
    this by passing a ``site``-carrying request: the stock index view calls
    ``get_current_site(request)``; we shadow it by setting ``request`` attributes
    the view reads. Since the stock view reads ``request.scheme`` +
    ``get_current_site(request).domain``, we override both via a lightweight
    request proxy.
    """

    def render():
        host, scheme = _canonical_host_and_scheme()
        proxied = _CanonicalRequest(request, host=host, scheme=scheme)
        return _django_sitemap_index(
            proxied,
            SITEMAPS,
            sitemap_url_name="discovery-sitemap-section",
        )

    return _cached_document("sitemap-index", request, render)


@require_GET
@cache_control(public=True, max_age=3600)
def sitemap_section(request, section) -> HttpResponse:
    """A child sitemap for one record type (canonical IRIs are emitted verbatim).

    Child ``<loc>`` values are the resources' canonical ``@id`` IRIs (the
    ``_CorpusSitemap.get_urls`` override emits them verbatim), so the request
    Host is irrelevant here — but we still server-cache the rendered page.
    """

    def render():
        return _django_sitemap_section(request, SITEMAPS, section=section)

    return _cached_document(f"sitemap-section:{section}", request, render)


class _CanonicalRequest:
    """A thin request proxy forcing ``scheme`` + ``get_host()`` to the canonical
    authority, so the wrapped Django sitemap index view builds child links on the
    platform host rather than the (spoofable) request Host header.
    """

    def __init__(self, request, *, host: str, scheme: str):
        self._request = request
        self._host = host
        self._scheme = scheme

    @property
    def scheme(self) -> str:
        return self._scheme

    def get_host(self) -> str:
        return self._host

    def build_absolute_uri(self, location=None):
        from urllib.parse import urljoin

        root = f"{self._scheme}://{self._host}"
        if location is None:
            location = self._request.get_full_path()
        return urljoin(root, location)

    def __getattr__(self, name):
        # Everything else (GET, path, META, etc.) delegates to the real request.
        return getattr(self._request, name)


# ── ResourceSync documents ───────────────────────────────────────────────────


@require_GET
@cache_control(public=True, max_age=3600)
def resourcesync_source_description(request) -> HttpResponse:
    """Served at ``/.well-known/resourcesync`` — the ResourceSync entry point."""
    return _xml_response(resourcesync.source_description())


@require_GET
@cache_control(public=True, max_age=3600)
def resourcesync_capability_list(request) -> HttpResponse:
    return _xml_response(resourcesync.capability_list())


@require_GET
@cache_control(public=True, max_age=3600)
def resourcesync_resource_list(request) -> HttpResponse:
    """The full corpus enumeration — server-cached (it scans every type)."""
    return _cached_document(
        "resourcesync-resourcelist",
        request,
        lambda: _xml_response(resourcesync.resource_list()),
    )
