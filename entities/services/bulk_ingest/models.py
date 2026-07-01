"""Data models for the bulk-ingestion service.

These are the I/O shapes for :class:`~entities.services.bulk_ingest.ingest.BulkIngestService`:

- :class:`IngestRecord` — one input record (an entity payload plus its sources).
- :class:`IngestSource` — a single source attribution carried by a record. This is
  the *hook* for the ≥2-source rule; the service only counts sources here, it does
  not (yet) verify them.
- :class:`IngestStatus` — the outcome of a single record.
- :class:`IngestRecordError` — a per-record failure (index, slug, message).
- :class:`BulkIngestResult` — the structured result of an ``ingest_entities`` call.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Nepal ccTLD second-level domains: a registration like ``ecn.gov.np`` lives
# *under* one of these multi-part suffixes, so the registrable domain (eTLD+1)
# is the last THREE labels (e.g. ``results.ecn.gov.np`` -> ``ecn.gov.np``).
# This is the small, well-known subset relevant to Nepali public entities; it
# avoids pulling in ``tldextract`` (and its runtime public-suffix-list download)
# for a gate that only ever sees ``*.np`` and ordinary TLDs in practice.
_NP_SECOND_LEVEL_SUFFIXES = frozenset(
    {"gov", "com", "edu", "org", "net", "mil"}
)


def _registrable_domain(host: str) -> Optional[str]:
    """Reduce a hostname to its registrable domain (approx. eTLD+1).

    Collapses subdomains of the same organisation to a single key so that, e.g.,
    ``results.ecn.gov.np`` and ``data.ecn.gov.np`` both map to ``ecn.gov.np``.

    Rules:
    - ``*.np`` with a recognised second-level suffix (``gov``/``com``/``edu``/
      ``org``/``net``/``mil``): keep the last **three** labels
      (``ecn.gov.np``), never collapsing to the bare suffix (``gov.np``).
    - any other ``*.np`` (e.g. ``foo.bar.np``): fall back to the last **three**
      labels as a conservative eTLD+1 guess for the ccTLD.
    - ordinary TLDs (``.org``, ``.com``, ...): keep the last **two** labels
      (``results.example.org`` -> ``example.org``).

    Limitation: this is a heuristic, not a full public-suffix-list lookup. It is
    correct for the Nepal multi-part suffixes above and for ordinary two-label
    TLDs, but does not handle every multi-part suffix worldwide (e.g.
    ``co.uk``). Callers in this gate only see ``*.np`` and plain TLDs, so the
    approximation is safe here; broaden via ``tldextract`` if that changes.
    """
    host = host.strip().strip(".").lower()
    if not host:
        return None
    labels = host.split(".")
    if len(labels) <= 1:
        # No dot (bare token / localhost): not a real registrable domain.
        return host if "." in host else host or None
    if labels[-1] == "np":
        # Nepal ccTLD: registrations sit under a multi-part suffix.
        if len(labels) >= 3 and labels[-2] in _NP_SECOND_LEVEL_SUFFIXES:
            return ".".join(labels[-3:])
        # Bare ``x.np`` or unknown second level -> use up to last 3 labels.
        return ".".join(labels[-3:]) if len(labels) >= 3 else ".".join(labels)
    # Ordinary TLD: registrable domain is the last two labels.
    return ".".join(labels[-2:])


class IngestStatus(str, Enum):
    """Outcome of a single record in a bulk-ingest run."""

    CREATED = "created"
    """The entity did not exist and was written as a new entity (version 1)."""

    UPDATED = "updated"
    """The entity already existed and was upserted as a new version."""

    HELD = "held"
    """Staged, not published — failed the >=2-source gate (see sourcing-plan §1).

    A held record is validated and a candidate id is computed, but it is NOT
    written to the ``entities`` table. It is reported back so the caller (or a
    later source-verification pass) can resolve it.
    """

    FAILED = "failed"
    """The record failed validation or construction; nothing was written."""


@dataclass
class IngestSource:
    """A single source attribution attached to an ingest record.

    This is intentionally minimal: the ≥2-source rule (sourcing-plan §1) is
    enforced here only as a *count* gate. Real source verification (primary vs.
    independent corroborator, URL liveness, ID anchoring) is a separate engine
    that will consume these objects later — this is the interface it will use.
    """

    url: Optional[str] = None
    """Where the claim was sourced from (official portal, gazette, news, ...)."""

    title: Optional[str] = None
    """Human-readable label for the source."""

    kind: Optional[str] = None
    """Free-form role hint, e.g. "primary" / "corroborator" — not validated yet."""

    authority: Optional[str] = None
    """The publisher/authority behind this source (e.g. ``ecn.gov.np``, ``nepalgazette``).

    This is the *independence* key for the ≥2-source HOLD gate: two sources from
    the **same** authority do not corroborate one another. When set, it is used
    verbatim to identify the publisher; when absent, the gate falls back to the
    registrable domain of the source URL. See :meth:`IngestSource.publisher_key`.
    """

    def publisher_key(self) -> Optional[str]:
        """Return the independence key used to de-dup sources by publisher.

        Prefers an explicit :attr:`authority`; otherwise derives the
        **registrable domain** (eTLD+1) from :attr:`url`. Reducing to the
        registrable domain means subdomains of the same authority collapse to
        one key — ``results.ecn.gov.np`` and ``data.ecn.gov.np`` both yield
        ``ecn.gov.np`` and therefore do NOT corroborate one another — while
        genuinely different domains stay distinct.

        Scheme-less URLs are tolerated: ``urlparse('ecn.gov.np/results')``
        yields no hostname, so we retry with a ``//`` prefix so the bare host is
        recovered instead of being silently dropped (which would wrongly HOLD).

        Returns ``None`` only when neither an authority nor a parseable URL host
        is available — such a source cannot establish independence and is
        ignored by the gate (it does not count toward the distinct-publisher
        tally).
        """
        if self.authority:
            return self.authority.strip().lower()
        if self.url:
            url = self.url.strip()
            host = (urlparse(url).hostname or "").lower()
            if not host:
                # Scheme-less string like ``ecn.gov.np/results``: urlparse
                # treats the whole thing as a path, so retry as a network
                # location by prepending ``//``.
                host = (urlparse("//" + url).hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            return _registrable_domain(host) if host else None
        return None

    @classmethod
    def from_obj(cls, obj: Any) -> "IngestSource":
        """Coerce a raw record-source (dict or plain string URL) to IngestSource."""
        if isinstance(obj, IngestSource):
            return obj
        if isinstance(obj, str):
            return cls(url=obj)
        if isinstance(obj, dict):
            return cls(
                url=obj.get("url"),
                title=obj.get("title"),
                kind=obj.get("kind") or obj.get("role"),
                authority=obj.get("authority") or obj.get("publisher"),
            )
        raise ValueError(f"Unsupported source value: {obj!r}")


@dataclass
class IngestRecord:
    """One bulk-ingest input record: an entity payload plus its sources.

    Args:
        entity_prefix: N-level classification prefix (e.g. ``person`` or
            ``organization/political_party``) for the authoring shape. Optional
            when the payload is a full JSON-LD doc (``@id`` carries the prefix).
        entity_data: The entity payload — either the authoring shape (``prefix``/
            ``slug``/``type``/``name``/…) or a full schema.org JSON-LD doc
            (``@id``/``@type``/``name``/…). Normalized by ``normalize_authoring_payload``.
        sources: Source attributions backing this entity. The ≥2-source gate counts
            these; fewer than ``min_sources`` => the record is HELD.
    """

    entity_prefix: Optional[str]
    entity_data: Dict[str, Any]
    sources: List[IngestSource] = field(default_factory=list)

    @classmethod
    def from_dict(cls, obj: Dict[str, Any]) -> "IngestRecord":
        """Build a record from a raw JSON/JSONL object.

        Accepts ``entity_prefix`` either at top level or inside ``entity_data``;
        a full JSON-LD payload (carrying ``@id``) needs no separate prefix.
        Sources may be a list of dicts or bare URL strings.
        """
        if not isinstance(obj, dict):
            raise ValueError(f"Record must be a JSON object, got {type(obj).__name__}")

        entity_data = dict(obj.get("entity_data") or {})
        # Allow the entity payload to be inlined alongside metadata keys.
        if not entity_data:
            entity_data = {
                k: v
                for k, v in obj.items()
                if k not in ("entity_prefix", "sources", "entity_data")
            }

        entity_prefix = obj.get("entity_prefix") or entity_data.get("entity_prefix")
        # A full JSON-LD payload (with @id) carries its own identity — no prefix
        # field needed. Otherwise the authoring shape must declare one.
        if not entity_prefix and "@id" not in entity_data:
            raise ValueError("Record must include 'entity_prefix' (or a full JSON-LD '@id')")

        raw_sources = obj.get("sources") or []
        sources = [IngestSource.from_obj(s) for s in raw_sources]
        return cls(
            entity_prefix=entity_prefix,
            entity_data=entity_data,
            sources=sources,
        )


@dataclass
class IngestRecordError:
    """A per-record failure in a bulk-ingest run."""

    index: int
    """0-based position of the record in the input batch."""

    slug: Optional[str]
    """The record's slug, if it could be read (for human triage)."""

    message: str
    """Why the record failed (validation message / exception text)."""


@dataclass
class BulkIngestResult:
    """Structured result of a :meth:`BulkIngestService.ingest_entities` call."""

    total: int = 0
    """Number of input records processed."""

    created: int = 0
    """Records written as new entities."""

    updated: int = 0
    """Records that upserted an existing entity."""

    held: int = 0
    """Records staged (not published) by the >=2-source gate."""

    deduped_in_batch: int = 0
    """Records collapsed into an earlier record sharing the same entity id.

    When a single batch contains two or more records that resolve to the *same*
    entity id, only the **first** (first-wins) is processed; the later copies
    are counted here and are NOT counted as created/updated. This keeps the
    reported counts equal to the number of distinct rows actually persisted
    (the set-based upsert would otherwise collapse them silently, inflating
    ``created``). See ``PILOT-RESULTS.md`` §6 issue 1.
    """

    failed: int = 0
    """Records that failed validation/construction."""

    dry_run: bool = False
    """Whether this run skipped all database writes."""

    held_ids: List[str] = field(default_factory=list)
    """Entity ids of held records (for a later verification pass)."""

    errors: List[IngestRecordError] = field(default_factory=list)
    """Per-record failures."""

    @property
    def written(self) -> int:
        """Records that resulted in a database write (created + updated)."""
        return self.created + self.updated

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable summary (for CLI ``--json`` output)."""
        return {
            "total": self.total,
            "created": self.created,
            "updated": self.updated,
            "held": self.held,
            "deduped_in_batch": self.deduped_in_batch,
            "failed": self.failed,
            "dry_run": self.dry_run,
            "held_ids": list(self.held_ids),
            "errors": [
                {"index": e.index, "slug": e.slug, "message": e.message}
                for e in self.errors
            ],
        }
