"""Load priority CIAA case numbers from the data file and filter querysets."""

import json
import logging
import os
from typing import Optional

from django.db.models import QuerySet

from jawafdehi_shared.entities.ids import build_courtcase_iri

from cases.models import Case

logger = logging.getLogger(__name__)

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "priority_cases.json"
)

# Courts a priority CIAA case number may sit in (first instance / appeal) —
# the same scope the legacy "special:"/"supreme:" prefix matching used.
_COURTS = ("special", "supreme")


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


def filter_by_priority(
    queryset: QuerySet[Case],
    priority_cases: Optional[list[str]] = None,
) -> QuerySet[Case]:
    """Filter a Case queryset to cases referencing any of the priority case numbers.

    Court-case references live on the CaseCourtCaseReference join as canonical
    @id IRIs (``https://<base>/courtcase/<court>/<case_number>``, lowercased),
    fully reconstructable from (court, number) — so the match is one indexable
    ``IN`` lookup over the exact IRIs for the special/supreme courts (the same
    scope the legacy prefix matching used), on any DB vendor.

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

    iris = {
        build_courtcase_iri(court, case_no)
        for court in _COURTS
        for case_no in priority_cases
    }
    # distinct(): a case referencing the number in BOTH courts joins twice.
    return queryset.filter(courtcase_references__courtcase_iri__in=iris).distinct()
