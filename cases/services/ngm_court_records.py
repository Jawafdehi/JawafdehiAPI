"""In-process access to NGM court-case records for Jawafdehi commands.

The standalone NGM service (and the backend REST proxy that forwarded to it)
were retired in the service consolidation. NGM's courts/cases now live as the
``courts`` app in this same project, routed to the ``ngm`` database
by ``config.db_router``. So Jawafdehi code that needs court-case ground truth
(e.g. the ``casework`` enrichers) reads it via the ORM in-process — no HTTP,
no SQL proxy.

``get_court_case_details`` preserves the shape the old proxy returned
(``{"case": {...}, "hearings": [...], "entities": [...]}``) so its callers and
their tests are unchanged. Verdict fields (``verdict_date_ad`` /
``verdict_judge``), which the relational ``CourtCase`` model carries in its
``extra_data`` JSON rather than as columns, are surfaced flat into the ``case``
dict to match the old SQL projection.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Verdict attributes the old proxy SELECTed as columns but which the relational
# CourtCase model keeps inside its extra_data JSON. Surface them flat so the
# returned `case` dict matches the previous projection.
_EXTRA_DATA_CASE_FIELDS = ("verdict_date_ad", "verdict_judge")


def get_court_case_details(
    court_identifier: str, case_number: str
) -> Optional[dict]:
    """Fetch one court case (with hearings + entities) from the NGM database.

    Returns ``None`` if the case is not found, else a dict with:
      * ``case``     — the case record (registration/verdict dates, status, …)
      * ``hearings`` — hearing rows, newest first
      * ``entities`` — party rows (plaintiffs/defendants), ordered by side/name

    Mirrors the contract of the retired ``ngm.services.get_court_case_details``
    proxy helper, but reads the ``courts`` models in-process.
    """
    # Imported lazily so this module is importable even where the NGM app isn't
    # loaded (e.g. isolated unit tests that monkeypatch this function).
    from courts.models import CaseEntity, CourtCase, CourtCaseHearing
    from courts.normalize import best_effort_normalize

    normalized = best_effort_normalize(case_number)
    case = (
        CourtCase.objects.filter(
            court_id=court_identifier, case_number=normalized
        ).first()
    )
    if case is None:
        return None

    extra = case.extra_data if isinstance(case.extra_data, dict) else {}
    case_data = {
        "case_number": case.case_number,
        "court_identifier": case.court_id,
        "registration_date_bs": case.registration_date_bs,
        "registration_date_ad": case.registration_date_ad,
        "case_type": case.case_type,
        "case_status": case.case_status,
        "plaintiff": case.plaintiff,
        "defendant": case.defendant,
        "nes_id": case.nes_id,
    }
    for field in _EXTRA_DATA_CASE_FIELDS:
        case_data[field] = extra.get(field)

    hearings = [
        {
            "hearing_date_bs": h.hearing_date_bs,
            "hearing_date_ad": h.hearing_date_ad,
            "bench": h.bench,
            "bench_type": h.bench_type,
            "judge_names": h.judge_names,
            "lawyer_names": h.lawyer_names,
            "serial_no": h.serial_no,
            "case_status": h.case_status,
            "decision_type": h.decision_type,
            "remarks": h.remarks,
        }
        for h in CourtCaseHearing.objects.filter(
            court_id=court_identifier, case_number=case.case_number
        ).order_by("-hearing_date_ad", "-id")
    ]

    entities = [
        {
            "side": e.side,
            "name": e.name,
            "address": e.address,
            "nes_id": e.nes_id,
        }
        for e in CaseEntity.objects.filter(
            court_id=court_identifier, case_number=case.case_number
        ).order_by("side", "name")
    ]

    return {"case": case_data, "hearings": hearings, "entities": entities}
