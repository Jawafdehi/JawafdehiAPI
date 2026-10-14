"""Each appeal stage's verdict and resolved parties, read from the court rows the stage cites."""

from __future__ import annotations

from typing import Any, Iterable

from cases.stages import STAGE_APPEAL
from courts import case_status as cs
from courts.models import CaseEntity, CourtCase
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


def _by_docket(iris: Iterable[str]) -> dict[tuple[str, str], list[str]]:
    """Each parseable IRI keyed by ``(court, case_number)`` as the court tables store them."""
    wanted: dict[tuple[str, str], list[str]] = {}
    for iri in set(iris):
        try:
            parsed = parse_courtcase_iri(iri)
        except ValueError:
            continue
        wanted.setdefault((parsed.court.lower(), parsed.case_number.upper()), []).append(iri)
    return wanted


def resolve_appeal_verdicts(iris: Iterable[str]) -> dict[str, str | None]:
    """``{iri: verdict_type}`` for every IRI given; None when there is no classified verdict."""
    iris = set(iris)
    resolved: dict[str, str | None] = dict.fromkeys(iris)
    wanted = _by_docket(iris)
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


def resolve_appeal_parties(iris: Iterable[str]) -> dict[str, list[str]]:
    """``{iri: [nes_id, ...]}`` -- the entities resolved onto each docket's party rows.

    Sorted and de-duplicated; an unresolved row is left out, so a docket nobody
    has matched yet maps to an empty list.
    """
    iris = set(iris)
    resolved: dict[str, set[str]] = {iri: set() for iri in iris}
    wanted = _by_docket(iris)
    if wanted:
        rows = (
            CaseEntity.objects.filter(
                court_id__in={court for court, _ in wanted},
                case_number__in={number for _, number in wanted},
            )
            .exclude(nes_id__isnull=True)
            .exclude(nes_id="")
            .values_list("court_id", "case_number", "nes_id")
        )
        for court, number, nes_id in rows:
            for iri in wanted.get((court.lower(), number.upper()), ()):
                resolved[iri].add(nes_id)
    return {iri: sorted(ids) for iri, ids in resolved.items()}
