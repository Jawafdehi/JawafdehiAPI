"""Load priority CIAA case numbers from the data file and filter querysets."""

import json
import logging
import os
from typing import Optional

from django.conf import settings
from django.db.models import Q, QuerySet

from cases.models import Case

logger = logging.getLogger(__name__)

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "priority_cases.json"
)

_COURT_PREFIXES = ("special:", "supreme:")


def load_priority_cases() -> list[str]:
    """Load all priority case numbers from the data file across all fiscal years.

    Returns a flat list of case numbers like ``["080-CR-0007", ...]``.
    """
    with open(_DATA_PATH) as f:
        data = json.load(f)

    cases = []
    for _, case_list in data.get("fiscal_years", {}).items():
        cases.extend(case_list)
    return cases


def _is_postgresql() -> bool:
    engine = settings.DATABASES.get("default", {}).get("ENGINE", "")
    return "postgresql" in engine


def filter_by_priority(
    queryset: QuerySet[Case],
    priority_cases: Optional[list[str]] = None,
) -> QuerySet[Case]:
    """Filter a Case queryset to only cases whose ``court_cases`` field
    contains any of the given priority case numbers.

    On PostgreSQL, uses ``court_cases__contains`` with JSON containment
    (the ``@>`` operator). On SQLite, falls back to Python-side filtering
    because SQLite does not support JSON array containment queries.

    Matching is prefix-agnostic: if the priority list contains ``080-CR-0007``
    it will match ``special:080-CR-0007``, ``supreme:080-CR-0007``, etc.

    Args:
        queryset: Base queryset (already filtered for CIAA DRAFT cases).
        priority_cases: List of priority case numbers. If ``None``, loads
            from the data file.

    Returns:
        Filtered queryset.
    """
    if priority_cases is None:
        priority_cases = load_priority_cases()

    if not priority_cases:
        logger.warning("Priority case list is empty — no cases will match")
        return queryset.none()

    if _is_postgresql():
        q = Q()
        for case_no in priority_cases:
            for prefix in _COURT_PREFIXES:
                q |= Q(court_cases__contains=[f"{prefix}{case_no}"])
        return queryset.filter(q)

    logger.info(
        "SQLite detected — filtering priority cases in Python (%d priority cases)",
        len(priority_cases),
    )
    return _filter_priority_python(queryset, priority_cases)


def _filter_priority_python(
    queryset: QuerySet[Case],
    priority_cases: list[str],
) -> QuerySet[Case]:
    matched_ids = []
    for case in queryset.only("id", "court_cases"):
        court_entries = case.court_cases or []
        for entry in court_entries:
            if not isinstance(entry, str) or ":" not in entry:
                continue
            _, case_no = entry.split(":", 1)
            if case_no in priority_cases:
                matched_ids.append(case.id)
                break
    return queryset.filter(id__in=matched_ids)
