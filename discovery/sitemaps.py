"""Sitemaps for the public corpus — the crawl/discovery layer.

Built on Django's ``django.contrib.sitemaps`` framework but driven entirely by
the shared :mod:`discovery.corpus` enumerator (the ``@id`` envelope), so
the sitemap exposes EXACTLY the same public IRIs as ResourceSync.

KEY ADAPTATION: a stock Django ``Sitemap`` calls ``location(item)`` and prefixes
it with the current site's domain (``get_domain``). Our ``location`` is ALREADY a
full canonical ``jawafdehi.org`` IRI (the resource's ``@id``), so we must NOT let
Django re-prefix it. We therefore subclass and override ``get_urls`` to emit the
IRI verbatim as ``loc`` (the canonical-authority rule from
``jawafdehi_shared.entities.ids`` owns the scheme+host, not the request Site).

LARGE-CORPUS HANDLING: the Sitemaps protocol caps a single sitemap at 50,000 URLs
/ 50 MB. Django's framework paginates a ``Sitemap`` (``limit`` URLs per page) and
the ``sitemap`` index view stitches the pages into a ``<sitemapindex>``. We expose
one ``Sitemap`` per record type; each paginates at :data:`SITEMAP_LIMIT`, so a
type with > 50k public IRIs is split across child sitemaps referenced from the
index at ``/sitemap.xml``.
"""

from __future__ import annotations

import math

from django.contrib.sitemaps import Sitemap
from django.core.paginator import Paginator

from . import corpus

#: Max URLs per child sitemap (Sitemaps spec hard cap is 50,000 / 50 MB).
SITEMAP_LIMIT = 50_000


#: Per-type changefreq advisory (the ONE source of truth shared by both the
#: Sitemap classes below and the ResourceSync resource list, so the two public
#: surfaces never disagree on the hint).
CHANGEFREQ_BY_TYPE: dict[str, str] = {
    corpus.TYPE_ENTITY: "weekly",
    corpus.TYPE_MATERIAL: "monthly",
    corpus.TYPE_COURTCASE: "weekly",
    corpus.TYPE_CASE: "weekly",
}
#: Fallback when a type has no explicit entry.
DEFAULT_CHANGEFREQ = "weekly"


class _CountOnlyPaginator(Paginator):
    """A Paginator that knows its total count WITHOUT materializing object_list.

    Django's sitemap ``index`` view only needs ``num_pages`` from the paginator;
    it never reads a page's ``object_list``. The stock ``Sitemap.paginator``
    builds ``Paginator(self._items(), limit)`` which materializes the entire
    corpus just to count it (8 full table scans across 3 DBs per /sitemap.xml).
    Here we feed the count from a cheap ``COUNT(*)`` aggregate instead, and only
    materialize the object_list if a page is actually requested (it isn't, for
    the index).
    """

    def __init__(self, items_loader, count, per_page):
        self._items_loader = items_loader
        self._count = count
        # object_list is loaded lazily; pass an empty list as a placeholder so
        # the base __init__ doesn't touch the real corpus.
        super().__init__([], per_page)

    @property
    def count(self) -> int:
        return self._count

    @property
    def num_pages(self) -> int:
        if self._count == 0:
            return 1  # an empty section is still ONE (empty) child sitemap.
        return math.ceil(self._count / self.per_page)

    def page(self, number):
        # Only hit here when a child sitemap page is actually served; materialize
        # the real items for accurate slicing then.
        self.object_list = self._items_loader()
        return super().page(number)


class _CorpusSitemap(Sitemap):
    """A Sitemap whose items are :class:`corpus.Resource` for ONE type.

    The resource ``iri`` is itself the canonical absolute URL, so ``get_urls`` is
    overridden to emit it verbatim (no Site-domain prefixing, no ``location()``
    join). Pagination, lastmod and changefreq come through the framework.
    """

    # priority is an advisory hint to crawlers (Sitemaps protocol).
    priority = 0.5
    #: The per-page URL cap (Django paginates items into pages of this size).
    limit = SITEMAP_LIMIT
    #: Subclasses set their corpus type token.
    corpus_type: str = ""

    @property
    def changefreq(self) -> str:
        # Per-type advisory, shared with ResourceSync via CHANGEFREQ_BY_TYPE.
        return CHANGEFREQ_BY_TYPE.get(self.corpus_type, DEFAULT_CHANGEFREQ)

    def items(self) -> list[corpus.Resource]:
        # ``Sitemap`` requires an indexable/sliceable sequence (it paginates with
        # Django's Paginator), so materialize this type's resources into a list.
        # Streaming ``.iterator()`` is preserved in corpus.iter_resources; the
        # list is built ONLY when a child sitemap PAGE is served (never for the
        # index, which uses the cheap count via ``paginator`` below).
        return list(corpus.iter_resources((self.corpus_type,)))

    @property
    def paginator(self) -> _CountOnlyPaginator:
        # Feed num_pages from a cheap COUNT(*) so the sitemap INDEX never
        # materializes items() just to count them (count_resources wired in).
        return _CountOnlyPaginator(self.items, corpus.count_resources((self.corpus_type,)), self.limit)

    def get_latest_lastmod(self):
        # Cheap MAX(updated_at) aggregate — the index calls this once per type;
        # the stock implementation would iterate every item() instead.
        return corpus.max_lastmod((self.corpus_type,))

    def lastmod(self, item: corpus.Resource):
        return item.lastmod

    def get_urls(self, page=1, site=None, protocol=None):
        """Emit each resource's canonical IRI verbatim as ``loc``.

        Bypasses the stock ``location()`` + ``get_domain()`` prefixing because the
        ``iri`` already carries the canonical scheme+host (the platform's
        single authority), which a per-request Site must not override.
        """
        from django.core.paginator import Paginator

        paginator = Paginator(self.items(), self.limit)
        page_obj = paginator.page(page)
        urls = []
        for item in page_obj.object_list:
            lastmod = self.lastmod(item)
            urls.append(
                {
                    "item": item,
                    "location": item.iri,
                    "lastmod": lastmod,
                    "changefreq": self.changefreq,
                    "priority": str(self.priority),
                }
            )
        return urls


class EntitySitemap(_CorpusSitemap):
    corpus_type = corpus.TYPE_ENTITY


class MaterialSitemap(_CorpusSitemap):
    corpus_type = corpus.TYPE_MATERIAL


class CourtCaseSitemap(_CorpusSitemap):
    corpus_type = corpus.TYPE_COURTCASE


class CaseSitemap(_CorpusSitemap):
    corpus_type = corpus.TYPE_CASE
    priority = 0.8  # published cases are the primary editorial content.


#: The sitemap section map consumed by the index + section views (urls.py). One
#: section per record type so a large type paginates into its own child sitemaps.
SITEMAPS = {
    corpus.TYPE_ENTITY: EntitySitemap,
    corpus.TYPE_MATERIAL: MaterialSitemap,
    corpus.TYPE_COURTCASE: CourtCaseSitemap,
    corpus.TYPE_CASE: CaseSitemap,
}
