"""Clean the 144 free-text tags on the 82 published cases down to the vocabulary.

Three buckets, and the third is the honest one.

**Delete.** Values that are not tags at all: court case numbers, bigo amounts, and the
terms policy §9 bans by name. Each is measured in the live corpus and each fails the basic
test of a tag, which is that it GROUPS cases. A case number is unique by construction. An
amount is unique by construction. ``Corruption`` sits on 49 of 82 cases and ``CIAA`` on
53, so they group everything, which is the same as grouping nothing.

**Map.** The fragmentation ``research/corpus-analysis.md`` §6–§7 enumerates: one concept
stored many ways. These become :class:`~case_tags.models.TagAlias` rows and the case's
stored value is rewritten to the canonical id.

**Leave.** Everything else, reported and untouched. This is most of the singleton tail,
and it is mostly geography, institutions and people — values that belong on axes fed by
the ``entities`` relation, not by this vocabulary. Deleting them would lose real
information to make a number look better; the tagger's own pass is what supersedes them.

WHY THE MAP IS HAND-WRITTEN AND NOT DERIVED. No rule produces it. Nothing relates
``एनसेल`` to ``ncell``, and no edit distance says ``Assets Beyond Known Income`` and
``Illegal Property Acquisition`` are one concept while ``procurement-irregularity`` and
``bid-rigging`` are two. Every entry below is taken from the measured corpus analysis, so
the map is reviewable as a table rather than trusted as an algorithm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jawafdehi_shared.tags.normalize import normalize_tag

# A court/charge number: 081-CR-0098. Already the case's own identifier and its slug, so
# as a tag it is pure duplication — 21 such tags, 21 applications, zero shared values.
#
# The separator is hyphen OR whitespace, not hyphen only. The corpus spells these with
# hyphens, but the tagger reads case text where the same number appears as "081 CR 0098",
# and a guard that only catches the corpus spelling would let the model reintroduce the
# defect in the spelling it is most likely to produce.
CASE_NUMBER_RE = re.compile(r"\b\d{3}\s*[-\s]\s*[A-Za-z]{2}\s*[-\s]\s*\d{3,4}\b")

# A bigo amount: "~1 Crore 25 Lakh", "Rs 3.5 Crore", "रु ५० लाख". The `bigo` field holds
# the number and a range filter already ships, so these are a query concern. Every one is
# unique by construction and can never be shared between two cases.
# Hyphens are in the middle class deliberately: a slug-shaped amount ("1-crore-25-lakh")
# is exactly what a model asked for lowercase-kebab ids would emit, and the corpus spelling
# ("~1 Crore 25 Lakh") is what a caseworker typed. Both are the same defect.
AMOUNT_RE = re.compile(
    r"(\d|[०-९])" r"[-\s\d०-९.,]*" r"\s*(crore|lakh|karod|arab|रु|करोड|लाख|अरब)",
    re.IGNORECASE,
)

#: Banned by name (policy §9). Compared after normalization, so casing does not matter.
#:
#: The first two are the interesting ones. ``Corruption`` and ``CIAA`` are the corpus's
#: two most-used tags — 49 and 53 cases — and both are *removed* rather than mapped,
#: because 79 of 82 cases are already ``case_type: CORRUPTION`` and the CIAA is the filer
#: in nearly all of them. A tag that fits almost everything discriminates nothing. Both
#: remain available as derived facets from the fields that actually hold them.
#:
#: The rest are editorial judgements. Per decision D9 a tag states a fact about a case,
#: never our assessment of it: an evidentiary gap belongs in ``missing_details``, and a
#: stalled investigation is a status plus a sourced timeline entry.
BANNED_TERMS: frozenset[str] = frozenset(
    normalize_tag(v)
    for v in (
        "Corruption",
        "CIAA",
        "Special Court",
        "Supreme Court",
        "Unsubstantiated Claim",
        "Stalled Investigation",
        "Bagmati Mayor Involvement",
        "Corruption Allegation",
        "Hospital related",
        "national issue",
        "Irregular Amount",
        "High Specification",
        "Political Corruption",
    )
)

#: Raw corpus value -> canonical term id. From ``research/corpus-analysis.md`` §6–§7.
#:
#: Keys are written as they appear in the corpus and normalized on load, so this table
#: stays diffable against the analysis document rather than against a normalizer's output.
FRAGMENTATION: dict[str, str] = {
    # Illicit enrichment, stored seven ways across 31 applications (§6).
    "Illegal Property Acquisition": "illicit-enrichment",
    "Assets Beyond Known Income": "illicit-enrichment",
    "Illicit Enrichment": "illicit-enrichment",
    "Illegal Enrichment": "illicit-enrichment",
    "Illegal enrichment": "illicit-enrichment",
    "Illegal Property": "illicit-enrichment",
    "Illegal Wealth": "illicit-enrichment",
    # Procurement, four ways across 33 applications. `bid-rigging` is deliberately NOT
    # folded in — §8.1 reserves it for where collusion is specifically alleged, and
    # `Procurement Splitting` is a distinct technique, so both keep their own term.
    "Procurement Irregularities": "procurement-irregularity",
    "Procurement": "procurement-irregularity",
    "Public Procurement": "procurement-irregularity",
    "Bid Rigging": "bid-rigging",
    # Abuse of office, three ways. Note the trailing period on the third — the
    # normalizer strips it, so this entry is belt-and-braces for a value that arrives
    # from somewhere the normalizer has not run.
    "Public Office Abuse": "abuse-of-public-office",
    "Abuse of Authority": "abuse-of-public-office",
    "Abuse of Power.": "abuse-of-public-office",
    "Abuse of Power": "abuse-of-public-office",
    # Singular/plural and hyphenation pairs (§6).
    "Money Laundering": "money-laundering",
    "Money-laundering": "money-laundering",
    "Transport": "transport",
    "Transportation": "transport",
    # Cross-script duplicates. NOTHING derives these — they are the clearest case for a
    # hand-written table (§7).
    "Tax Evasion": "tax-evasion",
    "tax evasion": "tax-evasion",
    "कर छली": "tax-evasion",
    # Offence terms whose corpus spelling differs from the canonical label.
    "Embezzlement": "embezzlement",
    "Forged Documents": "forged-documents",
    "Revenue Leakage": "revenue-leakage",
    "Revenue": "revenue-leakage",
    "Land Management": "land-administration",
    "Land": "land-administration",
    "land-deal": "land-grab",
    "Land Scandel": "land-grab",  # typo in the corpus
    # Sector values that are already sector terms under another spelling.
    "Finance": "finance",
    "Forestry": "forestry",
    "Health": "health",
    "Education": "education",
    "Infrastructure": "infrastructure",
    "Construction": "infrastructure",
    "Water Supply": "water-supply",
    "Public Works": "infrastructure",
    "IT": "information-technology",
    "Agriculture": "agriculture",
    "Energy": "energy",
    # Governance level.
    "Local Government": "local-government",
}


@dataclass
class CleanupPlan:
    """What the cleanup would do, per raw value. Built without writing anything."""

    delete: dict[str, str] = field(default_factory=dict)  # raw -> reason
    remap: dict[str, str] = field(default_factory=dict)  # raw -> canonical id
    keep: list[str] = field(default_factory=list)  # raw, unresolved and left alone

    @property
    def touched(self) -> int:
        return len(self.delete) + len(self.remap)


def classify(raw: str) -> tuple[str, str]:
    """Decide what happens to one raw tag value.

    Returns ``(action, detail)`` where action is ``"delete"``, ``"remap"`` or ``"keep"``.

    Order matters: the banned checks run BEFORE the fragmentation map, so a value that is
    both (there is none today, but a future map entry could collide with a denylist entry)
    is deleted rather than mapped. Deleting a banned value is never wrong; mapping one
    would quietly reintroduce it under a canonical name.
    """
    value = normalize_tag(raw)
    if not value:
        return "delete", "empty after normalization"
    if CASE_NUMBER_RE.search(raw):
        return "delete", "court case number — already the case's identifier and slug"
    if AMOUNT_RE.search(raw):
        return "delete", "bigo amount — the `bigo` field holds it; unique per case"
    if value in BANNED_TERMS:
        return "delete", "banned by policy §9"

    canonical = _FRAGMENTATION_NORMALIZED.get(value)
    if canonical:
        return "remap", canonical
    return "keep", "no vocabulary term matched"


_FRAGMENTATION_NORMALIZED: dict[str, str] = {
    normalize_tag(k): v for k, v in FRAGMENTATION.items()
}


def plan(raw_values: list[str]) -> CleanupPlan:
    """Classify a whole corpus of raw values without touching the database."""
    out = CleanupPlan()
    for raw in dict.fromkeys(raw_values):  # de-dup, order-preserving
        action, detail = classify(raw)
        if action == "delete":
            out.delete[raw] = detail
        elif action == "remap":
            out.remap[raw] = detail
        else:
            out.keep.append(raw)
    return out
