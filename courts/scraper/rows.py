"""Shared result types for the court-portal parsers.

Every court's cause-list parser yields ``(ParsedCase, ParsedHearing)`` pairs and
every enrichment parser yields a :class:`ParsedEnrichment`, so the management
command's ORM-upsert path is court-agnostic. Court-owned typed columns are set as
fields; low-value legacy fields the v2 ``court_cases`` projection does not carry
(``division``/``category``/``section``/…) go into ``extra_data``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class ParsedCase:
    case_number: str
    court_identifier: str
    registration_date_bs: str | None = None
    registration_date_ad: date | None = None
    case_type: str | None = None
    plaintiff: str | None = None
    defendant: str | None = None
    extra_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedHearing:
    case_number: str
    court_identifier: str
    hearing_date_bs: str
    hearing_date_ad: date | None
    bench: str | None = None
    bench_type: str | None = None
    serial_no: str | None = None
    judge_names: str | None = None
    lawyer_names: str | None = None
    case_status: str | None = None
    decision_type: str | None = None
    remarks: str | None = None
    extra_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedEnrichment:
    """One case's detail-page enrichment: typed columns + extra_data + parties.

    ``core_fields`` holds court-owned typed columns to write (case_status,
    verdict_*, case_subject, hearing_count, …). ``extra_data`` is merged into the
    row's JSON (never replacing it). ``entities`` are ``{"side","name","address"}``
    dicts (side = "plaintiff" | "defendant").
    """

    core_fields: dict[str, Any] = field(default_factory=dict)
    extra_data: dict[str, Any] = field(default_factory=dict)
    entities: list[dict[str, Any]] = field(default_factory=list)

    def identifies_a_case(self) -> bool:
        """Did this parse actually find a case, or is it a not-found/error page?

        Deliberately ignores ``extra_data``: the supreme/district/high parsers
        always emit their ``enrichment_hearings``/``enrichment_timeline`` keys,
        so an EMPTY parse still yields a truthy ``extra_data`` dict. Testing
        truthiness of the whole payload therefore passes a not-found page —
        which would mark the case enriched (so it never retries) and drop its
        parties. Only ``core_fields`` and ``entities`` distinguish a real hit;
        verified against captured not-found fixtures for all four portals.
        """
        return bool(self.core_fields) or bool(self.entities)
