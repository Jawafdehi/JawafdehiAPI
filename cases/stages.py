"""A case's proceeding stages: the vocabulary, the record rules, the derived dates.

A case is a container of proceedings, not one proceeding. The published corpus
already holds a case with 12 dockets across 8 courts on three tiers, and a draft
with five first instances filed at three courts on one day, so the shape has to
be a list rather than a fixed pair of dates.

Dates are AD only; Bikram Sambat stays derived on display.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable

STAGE_INVESTIGATION = "investigation"
STAGE_INITIAL = "initial"
STAGE_APPEAL = "appeal"
STAGE_REVIEW = "review"
STAGE_OTHER = "other"

#: Ordered for display and error messages, not for validation -- see
#: ``validate_stages`` on why stages carry no global order.
STAGE_VALUES: tuple[str, ...] = (
    STAGE_INVESTIGATION,
    STAGE_INITIAL,
    STAGE_APPEAL,
    STAGE_REVIEW,
    STAGE_OTHER,
)

#: Stages that happen in a court. The derived dates count only these:
#: including ``investigation`` would move every case's archive sort position
#: backwards, silently changing the ordering the site has today.
COURT_STAGES = frozenset({STAGE_INITIAL, STAGE_APPEAL, STAGE_REVIEW})

_ALLOWED_KEYS = frozenset(
    {"stage", "start", "end", "courtcase_iri", "body", "label", "notes"}
)

NOTES_MAX_CHARS = 500

#: ``body`` names an investigating or arbitrating forum and ``label`` names an
#: ``other`` stage; both are PUBLIC (they render beside the dates) and both
#: live inside a JSONField, which has no length of its own. Bounded for the
#: same reason ``notes`` is -- an unbounded public string in a JSON column is
#: how the free-text ``tags`` field drifted to 144 distinct values.
BODY_MAX_CHARS = 200
LABEL_MAX_CHARS = 200


class StageError(ValueError):
    """A stage list the writer must fix. Surfaces as 422, never dropped."""


def _parse_date(value: Any, field: str, index: int) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise StageError(
            f"stages[{index}].{field}: {value!r} is not an AD date (YYYY-MM-DD)"
        ) from exc


def validate_stages(
    stages: Any, binds: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Return the stage list, or raise ``StageError`` naming the offending record.

    Unknown stages and unknown keys are refused rather than dropped: a
    caseworker who writes ``first_instance`` has to be told, or the stage simply
    never renders and nobody finds out.

    Records are validated individually. There is deliberately NO cross-record
    ordering rule: after a remand the new first instance starts *after* the
    appeal ended, and a case with parallel first instances is not linearly
    orderable at all, so any global ordering rule fires on correct data.

    ``binds`` distinguishes three states, and the difference is load-bearing:
    ``None`` means the caller has no case in hand and the ``courtcase_iri``
    rule is NOT checked (the serializer validates a document, not a case);
    a list -- including an EMPTY one -- means it is checked against exactly
    that set, so a case with no binds rejects every IRI. Collapsing ``None``
    into ``[]`` made the serializer reject every stage that cites a court
    case, whatever the case was actually bound to.
    """
    if not isinstance(stages, list):
        raise StageError("stages must be a list of stage records")

    check_binds = binds is not None
    bind_set = set(binds or ())
    validated: list[dict[str, Any]] = []

    for index, raw in enumerate(stages):
        if not isinstance(raw, dict):
            raise StageError(f"stages[{index}]: each stage must be an object")
        # Rebuilt with str keys so the rest of the function (and every caller
        # reading the returned list) has a concretely typed record.
        record: dict[str, Any] = {str(key): value for key, value in raw.items()}

        unknown = sorted(set(record) - _ALLOWED_KEYS)
        if unknown:
            raise StageError(
                f"stages[{index}]: unknown key(s) {', '.join(unknown)}; "
                f"allowed: {', '.join(sorted(_ALLOWED_KEYS))}"
            )

        stage = record.get("stage")
        if stage not in STAGE_VALUES:
            raise StageError(
                f"stages[{index}].stage: {stage!r} is not one of "
                f"{', '.join(STAGE_VALUES)}"
            )

        label = record.get("label")
        if stage == STAGE_OTHER and not (label and str(label).strip()):
            raise StageError(f"stages[{index}].label: required when stage is 'other'")
        if stage != STAGE_OTHER and label:
            raise StageError(
                f"stages[{index}].label: only meaningful when stage is 'other'"
            )

        start = _parse_date(record.get("start"), "start", index)
        end = _parse_date(record.get("end"), "end", index)
        # Equality is allowed: the charge sheet closes the investigation and
        # opens the court stage on the same day.
        if start and end and end < start:
            raise StageError(
                f"stages[{index}].end: {end} is before the stage start {start}"
            )

        iri = record.get("courtcase_iri")
        # No truthiness guard on ``bind_set``: once the caller HAS passed a
        # list, a case with no binds has nothing a stage could legitimately
        # point at, so an IRI there is still wrong. ``check_binds`` is what
        # separates that from "the caller cannot see the case at all".
        if iri and check_binds and iri not in bind_set:
            raise StageError(
                f"stages[{index}].courtcase_iri: {iri} is not one of this "
                "case's court_cases"
            )

        for field, cap in (
            ("notes", NOTES_MAX_CHARS),
            ("body", BODY_MAX_CHARS),
            ("label", LABEL_MAX_CHARS),
        ):
            text = record.get(field)
            if text is not None and len(str(text)) > cap:
                raise StageError(
                    f"stages[{index}].{field}: {len(str(text))} characters, "
                    f"maximum {cap}"
                )

        validated.append(record)

    return validated


def derived_proceeding_dates(stages: Any) -> tuple[date | None, date | None]:
    """``(proceedings_started_on, proceedings_decided_on)`` for a stage list.

    The start is the earliest known start among court stages; a stage with no
    start is skipped, because a verdict date is often known from a court order
    when the registration date is not.

    The decision is the latest end among court stages, and NULL while ANY court
    stage is still open -- a case with five parallel first instances is not
    decided when the first of them finishes.

    Pure and total: never raises, so a legacy row migrated unchanged (and
    flagged through ``missing_details``) still renders.
    """
    if not isinstance(stages, list):
        return (None, None)

    starts: list[date] = []
    ends: list[date] = []
    any_open = False

    for record in stages:
        if not isinstance(record, dict) or record.get("stage") not in COURT_STAGES:
            continue
        try:
            start = _parse_date(record.get("start"), "start", 0)
            end = _parse_date(record.get("end"), "end", 0)
        except StageError:
            continue
        if start:
            starts.append(start)
        if end:
            ends.append(end)
        else:
            any_open = True

    return (
        min(starts) if starts else None,
        None if any_open or not ends else max(ends),
    )


def first_instance_dates(stages: Any) -> tuple[str | None, str | None]:
    """``(start, end)`` of the single ``initial`` stage, for the read aliases.

    Returns ``(None, None)`` when there is no first instance, or more than one:
    the deprecated scalar shape cannot represent parallel first instances, so
    inventing an answer would be worse than admitting it has none.
    """
    if not isinstance(stages, list):
        return (None, None)
    initial = [
        s for s in stages if isinstance(s, dict) and s.get("stage") == STAGE_INITIAL
    ]
    if len(initial) != 1:
        return (None, None)
    start = initial[0].get("start")
    end = initial[0].get("end")
    return (str(start) if start else None, str(end) if end else None)


def apply_legacy_date(stages: Any, key: str, value: Any) -> list[dict[str, Any]]:
    """Write a deprecated ``case_start_date`` / ``case_end_date`` into the stages.

    The deployed SPA admin emits those two paths. They addressed columns; the
    dates now live on a record inside a list, and RFC-6902 ``replace`` on a
    path that does not exist is an error -- so this is a transform, not a path
    rewrite. The first instance is created when the case has none.

    Raises ``StageError`` when the case has more than one first instance: the
    old form cannot express that case, so guessing which stage it meant would
    corrupt data.
    """
    if key not in ("start", "end"):
        raise StageError(f"{key!r} is not a legacy date field")

    working = [dict(s) for s in stages if isinstance(s, dict)] if isinstance(stages, list) else []
    initial = [s for s in working if s.get("stage") == STAGE_INITIAL]

    if len(initial) > 1:
        raise StageError(
            "this case has more than one first-instance stage, so the "
            "deprecated date field cannot say which one it means -- patch "
            "/dates instead"
        )

    if not initial:
        if value in (None, ""):
            return working
        target = {"stage": STAGE_INITIAL}
        working.append(target)
    else:
        target = initial[0]

    if value in (None, ""):
        target.pop(key, None)
    else:
        target[key] = str(value)
    return working
