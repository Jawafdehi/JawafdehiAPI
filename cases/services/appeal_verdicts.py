"""Each appeal stage's verdict, read from the court row the stage cites."""

from __future__ import annotations

from typing import Any, Iterable

from cases.stages import STAGE_APPEAL
from courts import case_status as cs
from courts.models import CourtCase
from jawafdehi_shared.entities.ids import parse_courtcase_iri


def appeal_iris(dates: Any) -> list[str]:
    """The ``courtcase_iri`` of every appeal stage, in stage order."""
    stages = dates.get("stages") if isinstance(dates, dict) else None
    if not isinstance(stages, list):
        return []
    return [
        s["courtcase_iri"]
        for s in stages
        if isinstance(s, dict) and s.get("stage") == STAGE_APPEAL and s.get("courtcase_iri")
    ]


def resolve_appeal_verdicts(iris: Iterable[str]) -> dict[str, str | None]:
    """``{iri: verdict_type}`` for every IRI given; None when there is no classified verdict."""
    wanted: dict[tuple[str, str], list[str]] = {}
    resolved: dict[str, str | None] = {}
    for iri in set(iris):
        resolved[iri] = None
        try:
            parsed = parse_courtcase_iri(iri)
        except ValueError:
            continue
        wanted.setdefault((parsed.court.lower(), parsed.case_number.upper()), []).append(iri)

    if not wanted:
        return resolved

    rows = CourtCase.objects.filter(
        court_id__in={court for court, _ in wanted},
        case_number__in={number for _, number in wanted},
        is_deleted=False,
    ).values_list("court_id", "case_number", "verdict_type")

    for court, number, verdict in rows:
        # Same filter as the court-case endpoint: raw portal text is not a verdict.
        value = (verdict or "").strip()
        for iri in wanted.get((court.lower(), number.upper()), ()):
            resolved[iri] = value if value in cs.VERDICT_TYPES else None
    return resolved
