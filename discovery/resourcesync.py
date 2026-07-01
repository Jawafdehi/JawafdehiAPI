"""ResourceSync (ANSI/NISO Z39.99-2017) document generation — the harvest /
federation layer for the public corpus.

ResourceSync is a Sitemaps EXTENSION: every document is a ``<urlset>`` (or
``<sitemapindex>``) in the Sitemaps namespace, augmented with the ResourceSync
``rs:`` namespace (``http://www.openarchives.org/rs/terms/``) carrying ``<rs:md>``
(capability/metadata) and ``<rs:ln>`` (typed links). Like the sitemap, every
document here is driven by the shared :mod:`discovery.corpus` enumerator
(the ``@id`` envelope), so ResourceSync and the Sitemap describe the SAME corpus.

MVP SCOPE (this module): the three documents a harvester needs to do a baseline
sync of the whole corpus —

  * **Source Description** (``capability="description"``) — the entry point,
    served at ``/.well-known/resourcesync``. Points at the Capability List.
  * **Capability List** (``capability="capabilitylist"``) — advertises which
    capabilities this source offers (here: ``resourcelist``).
  * **Resource List** (``capability="resourcelist"``) — the full enumeration of
    every public IRI, with ``<lastmod>``/``<changefreq>`` and, where the platform
    serves schema.org JSON-LD for the resource, an ``<rs:ln rel="describedby">``
    pointing at that representation.

FUTURE (NOT in MVP): a **Change List** / **Change Dump** (incremental sync) and a
**Resource Dump** (bundled content). The spec lets a source add these later
without changing the baseline; the Capability List would simply advertise the new
capabilities. The Resource List + Capability List + Source Description trio is the
documented minimum for a harvestable source.

Engine-agnostic + sqlite-testable: pure XML serialization over the corpus
enumerator; no OpenSearch, no DB specifics.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

from . import corpus

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
RS_NS = "http://www.openarchives.org/rs/terms/"

#: Path the Source Description is served at (the ResourceSync well-known entry
#: point, per the spec's recommendation).
WELL_KNOWN_PATH = "/.well-known/resourcesync"
#: Public paths of the capability documents (used for cross-linking via rs:ln).
CAPABILITYLIST_PATH = "/resourcesync/capabilitylist.xml"
RESOURCELIST_PATH = "/resourcesync/resourcelist.xml"


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _rs_md(capability: str, *, at: datetime | None = None, extra: dict | None = None) -> str:
    """One ``<rs:md>`` element with a ``capability`` (+ optional attrs)."""
    attrs = [f'capability={quoteattr(capability)}']
    if at is not None:
        attrs.append(f'at={quoteattr(_iso(at))}')
    for key, value in (extra or {}).items():
        attrs.append(f"{key}={quoteattr(str(value))}")
    return f"  <rs:md {' '.join(attrs)}/>"


def _rs_ln(rel: str, href: str) -> str:
    """One ``<rs:ln>`` typed link (rel + href)."""
    return f'  <rs:ln rel={quoteattr(rel)} href={quoteattr(href)}/>'


def _abs(base: str, path: str) -> str:
    """Join the canonical IRI base with an absolute path."""
    return f"{base.rstrip('/')}{path}"


# ── Source Description (/.well-known/resourcesync) ───────────────────────────


def source_description(base: str | None = None) -> str:
    """The Source Description: the harvester's entry point.

    A ``<urlset>`` with ``capability="description"`` whose single ``<url>`` points
    (via ``rs:ln rel="describedby"``/``capability``) at the Capability List.
    """
    from jawafdehi_shared.entities.ids import iri_base

    base = base or iri_base()
    capabilitylist = _abs(base, CAPABILITYLIST_PATH)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<urlset xmlns={quoteattr(SITEMAP_NS)} xmlns:rs={quoteattr(RS_NS)}>',
        _rs_md("description"),
        "  <url>",
        f"    <loc>{escape(capabilitylist)}</loc>",
        '    <rs:md capability="capabilitylist"/>',
        "  </url>",
        "</urlset>",
    ]
    return "\n".join(lines) + "\n"


# ── Capability List ──────────────────────────────────────────────────────────


def capability_list(base: str | None = None) -> str:
    """The Capability List: which capabilities this source offers.

    MVP advertises a single capability — the ``resourcelist``. (A future Change
    List would add one more ``<url capability="changelist">`` here.) Links ``up``
    to the Source Description per the spec.
    """
    from jawafdehi_shared.entities.ids import iri_base

    base = base or iri_base()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<urlset xmlns={quoteattr(SITEMAP_NS)} xmlns:rs={quoteattr(RS_NS)}>',
        _rs_md("capabilitylist"),
        _rs_ln("up", _abs(base, WELL_KNOWN_PATH)),
        "  <url>",
        f"    <loc>{escape(_abs(base, RESOURCELIST_PATH))}</loc>",
        '    <rs:md capability="resourcelist"/>',
        "  </url>",
        "</urlset>",
    ]
    return "\n".join(lines) + "\n"


# ── Resource List ──────────────────────────────────────────────────────────


def _resource_url_block(resource: corpus.Resource, base: str) -> list[str]:
    """The ``<url>`` block for one corpus resource (loc + lastmod + describedby)."""
    from .sitemaps import CHANGEFREQ_BY_TYPE, DEFAULT_CHANGEFREQ

    block = ["  <url>", f"    <loc>{escape(resource.iri)}</loc>"]
    lastmod = _iso(resource.lastmod)
    if lastmod:
        block.append(f"    <lastmod>{escape(lastmod)}</lastmod>")
    # Per-type changefreq, shared with the Sitemap classes (one source of truth)
    # so the two public surfaces never disagree on the crawl hint.
    changefreq = CHANGEFREQ_BY_TYPE.get(resource.type, DEFAULT_CHANGEFREQ)
    block.append(f"    <changefreq>{escape(changefreq)}</changefreq>")
    if resource.jsonld_url:
        href = (
            resource.jsonld_url
            if resource.jsonld_url.startswith(("http://", "https://"))
            else _abs(base, resource.jsonld_url)
        )
        # The schema.org JSON-LD representation of this resource.
        block.append(
            f'    <rs:ln rel="describedby" href={quoteattr(href)} '
            'type="application/ld+json"/>'
        )
    block.append("  </url>")
    return block


def resource_list(
    resources: Iterable[corpus.Resource] | None = None,
    *,
    base: str | None = None,
    at: datetime | None = None,
) -> str:
    """The Resource List: the full enumeration of the public corpus.

    ``<urlset>`` with ``<rs:md capability="resourcelist">``; one ``<url>`` per
    canonical IRI (loc = the IRI), with lastmod/changefreq and, where served, an
    ``rs:ln rel="describedby"`` to the schema.org JSON-LD. ``resources`` defaults
    to the full corpus enumerator (the ``@id`` envelope).
    """
    from django.utils import timezone
    from jawafdehi_shared.entities.ids import iri_base

    base = base or iri_base()
    if resources is None:
        resources = corpus.iter_resources()
    if at is None:
        at = timezone.now()

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<urlset xmlns={quoteattr(SITEMAP_NS)} xmlns:rs={quoteattr(RS_NS)}>',
        _rs_md("resourcelist", at=at),
        _rs_ln("up", _abs(base, CAPABILITYLIST_PATH)),
    ]
    for resource in resources:
        lines.extend(_resource_url_block(resource, base))
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"
