"""Public-search visibility gate for NGM court cases.

The ~1.6M-row court docket is otherwise fully public in unified search
(``/api/search/`` is ``AllowAny`` with no ACL — see ``search/service.py``). This
module decides, per court case, whether it belongs in the PUBLIC index: a curated
corruption / public-accountability slice, NOT a docket mirror. The call sites
(``reindex_courtcases``, ``Importer.reindex``, ``courts.signals``) gate on
``court_case_public_visible(case)`` — a hidden case is simply absent from
``ngm-courtcases``, mirroring the ``materials`` LISTED gate (``reindex_materials``).

Decision (2026-07-21). A court case is SHOWN iff — and a SENSITIVE type is NEVER
shown, overriding everything below:

1. its canonical case-type code is a financial-crime / accountability code
   (``SHOW_CODES``), OR
2. it sits in the corruption FORUM — Special Court or the CIAA ``CR`` series — and
   its code is not purely procedural (``PROCEDURAL_CODES``), OR
3. it is directly referenced by a PUBLISHED Jawafdehi case.

A ``case_type`` absent from the map is *unknown*: rule 1 fails (fail-closed on the
code axis), but forum / publish-link can still surface it.

The raw ``case_type`` → canonical code map lives in
``data/case_type_codes.json.gz`` (built offline; REGENERATE it if the importer's
``normalize_case_type`` changes, since the map is keyed on the stored value).
"""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path
from typing import Any

# ── canonical-code policy — the single source of truth for SHOW/HIDE ──────────
# Financial-crime / public-accountability codes that are public on their own.
SHOW_CODES = frozenset(
    {
        "CORRUPTION",
        "MONEY_LAUNDERING",
        "COOPERATIVE_FRAUD",
        "REVENUE_TAX_CUSTOMS",
        "FRAUD_CHEATING",
    }
)
# Legally/ethically protected personal matters — NEVER shown, even via a forum or
# published-case override (the "sensitive floor").
SENSITIVE_CODES = frozenset(
    {
        "SEXUAL_OFFENSE",
        "DOMESTIC_VIOLENCE",
        "DIVORCE",
        "MARRIAGE",
        "HUMAN_TRAFFICKING",
    }
)
# Generic / procedural buckets (petitions, enforcement, notarial, unspecified) —
# excluded even inside the corruption forum, which is ~76% procedural petitions.
PROCEDURAL_CODES = frozenset(
    {
        "MISC_PETITION",
        "INJUNCTION_INTERIM",
        "EXECUTION_ENFORCEMENT",
        "POWER_OF_ATTORNEY",
        "SCHEDULE_OFFENSE",
        "OTHER_CRIMINAL",
        "UNCATEGORIZED",
    }
)

_MAP_PATH = Path(__file__).resolve().parent / "data" / "case_type_codes.json.gz"
# CIAA case-number series (NNN-CR-NNNN), e.g. ``081-CR-0081`` (mirrors cases/models).
_CR_RE = re.compile(r"\d{3}-CR-\d{4}")

_code_map: dict[str, str] | None = None
_published_iris: frozenset[str] | None = None


def _load_map() -> dict[str, str]:
    """Load (once) the raw case_type → canonical code map from the gz asset."""
    global _code_map
    if _code_map is None:
        try:
            with gzip.open(_MAP_PATH, "rt", encoding="utf-8") as fh:
                _code_map = json.load(fh)
        except FileNotFoundError:
            _code_map = {}
    return _code_map


def case_type_code(case_type: str | None) -> str | None:
    """Canonical code for a stored ``case_type`` string, or ``None`` if unmapped."""
    if not case_type:
        return None
    return _load_map().get(case_type)


def is_cr_series(case_number: str | None) -> bool:
    """True if the case number is in the CIAA ``CR`` series (``NNN-CR-NNNN``)."""
    return bool(case_number and _CR_RE.search(case_number))


def in_corruption_forum(case: Any) -> bool:
    """True if the case is in the corruption forum: Special Court or CR-series."""
    if (getattr(case, "court_id", None) or "") == "special":
        return True
    return is_cr_series(getattr(case, "case_number", None))


def published_referenced_iris(*, refresh: bool = False) -> frozenset[str]:
    """Court-case IRIs directly referenced by a PUBLISHED Jawafdehi case (cached).

    Small set (dozens). Cached process-wide; ``clear_published_cache()`` (called
    from ``cases.signals`` on any Case state change) invalidates it, and the bulk
    reindex commands refresh it at start.
    """
    global _published_iris
    if refresh or _published_iris is None:
        try:
            from cases.models import CaseCourtCaseReference, CaseState

            _published_iris = frozenset(
                CaseCourtCaseReference.objects.filter(
                    case__state=CaseState.PUBLISHED
                ).values_list("courtcase_iri", flat=True)
            )
        except Exception:  # noqa: BLE001 — best-effort: a DB/import hiccup must not break indexing.
            _published_iris = frozenset()
    return _published_iris


def clear_published_cache() -> None:
    """Invalidate the published-reference cache (on Case publish-state change)."""
    global _published_iris
    _published_iris = None


def is_published_referenced(case: Any) -> bool:
    """True if this court case is directly referenced by a PUBLISHED Jawafdehi case."""
    iri = getattr(case, "iri", None)
    return bool(iri) and iri in published_referenced_iris()


def court_case_public_visible(case: Any) -> bool:
    """Whether a court case belongs in the PUBLIC unified-search index.

    See the module docstring for the rule. Pure/read-only except for the cached
    published-reference lookup; degrades to "not referenced" when that query is
    unavailable (e.g. a bare instance in a shaping test).
    """
    if getattr(case, "is_deleted", False):
        return False
    code = case_type_code(getattr(case, "case_type", None))
    if code in SENSITIVE_CODES:
        return False  # sensitive floor — overrides forum + publish-link
    if code in SHOW_CODES:
        return True
    if in_corruption_forum(case) and code not in PROCEDURAL_CODES:
        return True
    return is_published_referenced(case)
