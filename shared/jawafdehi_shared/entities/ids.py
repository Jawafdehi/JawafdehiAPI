"""Canonical entity/material id contract shared across the platform.

The platform join key is a schema.org **`@id` IRI**:
    https://jawafdehi.org/entity/<prefix>/<slug>            (entities — NES)
    https://jawafdehi.org/material/<source>/<ident>         (materials — NGM docs)
    https://jawafdehi.org/case/<slug>                       (Jawafdehi cases — minted at publish)
    https://jawafdehi.org/courtcase/<court>/<case_number>   (NGM court cases — composite key)

Clean-slate design: IRIs are the ONLY id form (no legacy ``entity:<prefix>/<slug>``,
no backfill — data is created fresh as schema.org JSON-LD keyed by these IRIs).

Authority policy (canonical-authority, enforced — not just on build): the IRI base
is overridable per deployment via the ``JAWAFDEHI_IRI_BASE`` env var, but a given
platform uses ONE base for everything it persists. The scheme+host is part of the
join key, so it MUST be canonical for two services to ever match. This module
therefore:

  * NORMALIZES on store via :func:`canonicalize_entity_iri` /
    :func:`canonicalize_material_iri` — any valid-shaped IRI (``http`` vs
    ``https``, a foreign host, a ``:port``) is re-emitted on the canonical
    :func:`iri_base` authority+scheme, keeping only its path grammar
    (prefix/slug or source/ident). So ``http://evil.com/entity/person/ram`` and
    ``https://x:8443/entity/person/ram`` both canonicalize to
    ``https://jawafdehi.org/entity/person/ram``.
  * ANCHORS on validate — :func:`is_valid_entity_iri` /
    :func:`is_valid_material_iri` are STRICT by default: they accept ONLY the
    canonical base (scheme+host == :func:`iri_base`). A validator for a join key
    must reject a non-canonical host, not silently accept it. Pass
    ``any_host=True`` for the lenient shape-only check (the path grammar without
    the host anchor) — used internally by the canonicalizers.

A :data:`MAX_IRI_LENGTH` bound is also enforced by the validators so a stored IRI
can never exceed the width of the consuming ``CharField`` columns (NGM/Jawafdehi
``nes_id``, NES ``StoredEntity.iri``).

Single source of truth so the services can't drift.
See think-big/nes-schemaorg-remodel-plan.md.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# The one canonical authority baked into every persisted @id. Configurable default
# (env override), but constant within a platform — NOT per-request/per-host.
DEFAULT_IRI_BASE = "https://jawafdehi.org"

# Upper bound on a stored IRI's length. The consuming join-key columns are
# ``CharField(300)`` (NES ``StoredEntity.iri``, NGM/Jawafdehi ``nes_id``,
# NGM ``Material.iri``), so an IRI longer than this could not be stored on every
# surface — reject it at the contract boundary rather than discover the
# truncation/DataError downstream.
MAX_IRI_LENGTH = 300


def iri_base() -> str:
    """The canonical IRI authority+scheme (no trailing slash). One value per platform."""
    return os.getenv("JAWAFDEHI_IRI_BASE", DEFAULT_IRI_BASE).rstrip("/")


def _is_canonical_authority(value: str) -> bool:
    """True iff ``value``'s scheme+host(+port) equals the canonical :func:`iri_base`."""
    base = urlsplit(iri_base())
    got = urlsplit(value)
    return (got.scheme, got.netloc) == (base.scheme, base.netloc)


# ── Entity ids ──────────────────────────────────────────────────────────────
# prefix: 1-4 slash-joined lowercase segments; slug: lowercase alphanumeric+hyphens.
_PREFIX = r"[a-z0-9_]+(?:/[a-z0-9_]+){0,3}"
_SLUG = r"[a-z0-9][a-z0-9-]*"
ENTITY_IRI_RE = re.compile(rf"^https?://[^/]+/entity/(?P<prefix>{_PREFIX})/(?P<slug>{_SLUG})$")


@dataclass(frozen=True)
class EntityId:
    prefix: str
    slug: str

    def iri(self, base: str | None = None) -> str:
        """The canonical schema.org @id IRI."""
        return f"{(base or iri_base())}/entity/{self.prefix}/{self.slug}"

    def __str__(self) -> str:
        return self.iri()


def is_valid_entity_iri(value: str, *, any_host: bool = False) -> bool:
    """True iff ``value`` is a valid entity @id IRI (.../entity/<prefix>/<slug>).

    STRICT by default: the scheme+host must equal the canonical :func:`iri_base`
    (a join-key validator must reject a non-canonical host). Pass
    ``any_host=True`` for a lenient shape-only check (path grammar only).
    The :data:`MAX_IRI_LENGTH` bound is enforced either way.
    """
    if not value or len(value) > MAX_IRI_LENGTH:
        return False
    if not ENTITY_IRI_RE.match(value):
        return False
    return any_host or _is_canonical_authority(value)


def parse_entity_iri(value: str) -> EntityId:
    """Parse an entity @id IRI into (prefix, slug). Raises ValueError if invalid.

    Accepts any valid-shaped IRI (host not anchored) so the canonicalizer can
    re-key a foreign-host IRI onto the canonical base.
    """
    m = ENTITY_IRI_RE.match(value or "")
    if not m:
        raise ValueError(f"Not a valid entity IRI: {value!r}")
    return EntityId(prefix=m.group("prefix"), slug=m.group("slug"))


def build_entity_iri(prefix: str, slug: str, base: str | None = None) -> str:
    """Format + validate a canonical entity @id IRI."""
    iri = EntityId(prefix=prefix, slug=slug).iri(base)
    if not is_valid_entity_iri(iri):
        raise ValueError(f"Invalid prefix/slug for entity IRI: {prefix!r}/{slug!r}")
    return iri


def canonicalize_entity_iri(value: str) -> str:
    """Re-emit any valid-shaped entity IRI on the canonical authority+scheme.

    Parses the ``/entity/<prefix>/<slug>`` path from ANY valid-shaped IRI
    (foreign host, ``http`` vs ``https``, ``:port``) and rebuilds it on the
    canonical :func:`iri_base`. So ``http://evil.com/entity/person/ram`` →
    ``https://jawafdehi.org/entity/person/ram``. The path grammar is validated;
    only the scheme+host is rewritten. Raises ValueError on a malformed path or
    if the canonical result exceeds :data:`MAX_IRI_LENGTH`.
    """
    parsed = parse_entity_iri(value)
    return build_entity_iri(parsed.prefix, parsed.slug)


# ── Material ids (NGM documents/manuscripts/legal corpus/reports) ────────────
# <base>/material/<source>/<ident> — source e.g. court/ciaa/ag/nkp/ppmo; ident is
# a source-natural id (case_number, press-release id, ...), slugged.
_SOURCE = r"[a-z0-9_]+(?:/[a-z0-9_]+){0,3}"
_IDENT = r"[a-z0-9][a-z0-9._-]*"
MATERIAL_IRI_RE = re.compile(rf"^https?://[^/]+/material/(?P<source>{_SOURCE})/(?P<ident>{_IDENT})$")


@dataclass(frozen=True)
class MaterialId:
    source: str
    ident: str

    def iri(self, base: str | None = None) -> str:
        return f"{(base or iri_base())}/material/{self.source}/{self.ident}"

    def __str__(self) -> str:
        return self.iri()


def is_valid_material_iri(value: str, *, any_host: bool = False) -> bool:
    """True iff ``value`` is a valid material @id IRI (.../material/<source>/<ident>).

    STRICT by default (scheme+host == canonical :func:`iri_base`); pass
    ``any_host=True`` for the lenient shape-only check. :data:`MAX_IRI_LENGTH`
    enforced either way.
    """
    if not value or len(value) > MAX_IRI_LENGTH:
        return False
    if not MATERIAL_IRI_RE.match(value):
        return False
    return any_host or _is_canonical_authority(value)


def build_material_iri(source: str, ident: str, base: str | None = None) -> str:
    iri = MaterialId(source=source, ident=ident).iri(base)
    if not is_valid_material_iri(iri):
        raise ValueError(f"Invalid source/ident for material IRI: {source!r}/{ident!r}")
    return iri


def parse_material_iri(value: str) -> MaterialId:
    """Parse any valid-shaped material IRI into (source, ident). Host not anchored."""
    m = MATERIAL_IRI_RE.match(value or "")
    if not m:
        raise ValueError(f"Not a valid material IRI: {value!r}")
    return MaterialId(source=m.group("source"), ident=m.group("ident"))


def canonicalize_material_iri(value: str) -> str:
    """Re-emit any valid-shaped material IRI on the canonical authority+scheme.

    Mirror of :func:`canonicalize_entity_iri` for ``/material/<source>/<ident>``.
    """
    parsed = parse_material_iri(value)
    return build_material_iri(parsed.source, parsed.ident)


# ── Case ids (Jawafdehi published cases) ─────────────────────────────────────
# <base>/case/<slug> — the public, canonical @id IRI for a Jawafdehi case. The
# slug is the case's external identifier. The grammar MUST match the
# authoritative case slug validator (services/jawafdehi/cases/validators.py
# ``validate_slug``): ``^[a-zA-Z][a-zA-Z0-9-]{0,49}$`` — i.e. starts with a
# letter, allows UPPERCASE, allows hyphens, and does NOT allow underscores.
# (A case slug like ``case-078-WC-0123-sunil-poudel`` is legal; if this grammar
# rejected it, Case.public_iri would raise on every read of that case.) Minted
# at PUBLISH: a case exposes this IRI only once state==PUBLISHED (see
# Case.public_iri); the IRI is derived from the slug, not stored separately.
_CASE_SLUG = r"[a-zA-Z][a-zA-Z0-9-]{0,49}"
CASE_IRI_RE = re.compile(rf"^https?://[^/]+/case/(?P<slug>{_CASE_SLUG})$")


@dataclass(frozen=True)
class CaseId:
    slug: str

    def iri(self, base: str | None = None) -> str:
        """The canonical schema.org @id IRI for a case."""
        return f"{(base or iri_base())}/case/{self.slug}"

    def __str__(self) -> str:
        return self.iri()


def is_valid_case_iri(value: str, *, any_host: bool = False) -> bool:
    """True iff ``value`` is a valid case @id IRI (.../case/<slug>).

    STRICT by default (scheme+host == canonical :func:`iri_base`); pass
    ``any_host=True`` for the lenient shape-only check. :data:`MAX_IRI_LENGTH`
    enforced either way.
    """
    if not value or len(value) > MAX_IRI_LENGTH:
        return False
    if not CASE_IRI_RE.match(value):
        return False
    return any_host or _is_canonical_authority(value)


def build_case_iri(slug: str, base: str | None = None) -> str:
    """Format + validate a canonical case @id IRI from a case slug."""
    iri = CaseId(slug=slug).iri(base)
    if not is_valid_case_iri(iri):
        raise ValueError(f"Invalid slug for case IRI: {slug!r}")
    return iri


def parse_case_iri(value: str) -> CaseId:
    """Parse any valid-shaped case IRI into (slug). Host not anchored.

    Raises ValueError if the path grammar is invalid.
    """
    m = CASE_IRI_RE.match(value or "")
    if not m:
        raise ValueError(f"Not a valid case IRI: {value!r}")
    return CaseId(slug=m.group("slug"))


def canonicalize_case_iri(value: str) -> str:
    """Re-emit any valid-shaped case IRI on the canonical authority+scheme.

    Mirror of :func:`canonicalize_entity_iri` for ``/case/<slug>``.
    """
    parsed = parse_case_iri(value)
    return build_case_iri(parsed.slug)


# ── Court-case ids (NGM CourtCase, composite key court + case_number) ─────────
# <base>/courtcase/<court>/<case_number> — a synthesized @id IRI for an NGM
# court case (whose natural key is the composite (case_number, court)). This is
# DISTINCT from the material @id IRI (``/material/court/<court>.<case_number>``,
# in ngm_service.materials.jsonld): the material IRI keys the CreativeWork
# JSON-LD record, whereas this courtcase IRI is a stable identifier for the
# court-case row itself. Derived from court+case_number; not stored.
# court: lowercase alphanumeric + underscore/hyphen (e.g. "supreme", "special").
# case_number: the source-natural number, slug-ish (e.g. "081-cr-0081").
_COURT = r"[a-z0-9][a-z0-9_-]*"
_CASE_NUMBER = r"[a-z0-9][a-z0-9._-]*"
COURTCASE_IRI_RE = re.compile(
    rf"^https?://[^/]+/courtcase/(?P<court>{_COURT})/(?P<case_number>{_CASE_NUMBER})$"
)


@dataclass(frozen=True)
class CourtCaseId:
    court: str
    case_number: str

    def iri(self, base: str | None = None) -> str:
        """The canonical synthesized @id IRI for an NGM court case."""
        return f"{(base or iri_base())}/courtcase/{self.court}/{self.case_number}"

    def __str__(self) -> str:
        return self.iri()


def is_valid_courtcase_iri(value: str, *, any_host: bool = False) -> bool:
    """True iff ``value`` is a valid court-case @id IRI (.../courtcase/<court>/<case_number>).

    STRICT by default (scheme+host == canonical :func:`iri_base`); pass
    ``any_host=True`` for the lenient shape-only check. :data:`MAX_IRI_LENGTH`
    enforced either way.
    """
    if not value or len(value) > MAX_IRI_LENGTH:
        return False
    if not COURTCASE_IRI_RE.match(value):
        return False
    return any_host or _is_canonical_authority(value)


def build_courtcase_iri(court: str, case_number: str, base: str | None = None) -> str:
    """Format + validate a canonical court-case @id IRI.

    ``court`` and ``case_number`` are lowercased so the IRI is stable and
    reconstructable from the relational (case_number, court) natural key,
    mirroring ``court_case_material_iri``.
    """
    iri = CourtCaseId(
        court=(court or "").lower(), case_number=(case_number or "").lower()
    ).iri(base)
    if not is_valid_courtcase_iri(iri):
        raise ValueError(
            f"Invalid court/case_number for court-case IRI: {court!r}/{case_number!r}"
        )
    return iri


def parse_courtcase_iri(value: str) -> CourtCaseId:
    """Parse any valid-shaped court-case IRI into (court, case_number). Host not anchored."""
    m = COURTCASE_IRI_RE.match(value or "")
    if not m:
        raise ValueError(f"Not a valid court-case IRI: {value!r}")
    return CourtCaseId(court=m.group("court"), case_number=m.group("case_number"))


def canonicalize_courtcase_iri(value: str) -> str:
    """Re-emit any valid-shaped court-case IRI on the canonical authority+scheme.

    Mirror of :func:`canonicalize_entity_iri` for ``/courtcase/<court>/<case_number>``.
    """
    parsed = parse_courtcase_iri(value)
    return build_courtcase_iri(parsed.court, parsed.case_number)
