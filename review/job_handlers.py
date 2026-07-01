"""Client-side job handlers for the jobs consumer (review_poller).

A handler is the WORKER-SIDE counterpart to a job ``kind``: given a claimed
job's ``payload`` (already enriched server-side by the kind's ``build_payload``
hook), it does the actual work locally and returns the result dict the consumer
submits back to ``/api/jobs/<id>/result/``.

Handler signature:
    handler(payload: dict, *, on_stage: Callable[[str], None]) -> dict

- ``on_stage(stage)`` — best-effort progress ping (extends the job lease).

This keeps ``review_poller`` domain-agnostic: to add a new consumable kind (e.g.
``material_convert``), register another handler here (or in the owning app) and
run the poller with ``--kinds material_convert``.
"""

from __future__ import annotations

from typing import Callable

from review import runner


def _handle_case_review(
    payload: dict,
    *,
    on_stage: Callable[[str], None],
) -> dict:
    """Run a casework review from a claimed ``case_review`` job payload.

    ``payload`` carries the server-resolved ``case`` dict and ``config`` (put
    there by the case_review kind's build_payload hook), so this runs with no DB
    access — exactly as the review poller always did.
    """
    case = payload.get("case")
    if not case:
        raise ValueError("case_review payload is missing the resolved 'case' dict.")
    config = payload.get("config")

    return runner.process_case(case, config, on_stage=on_stage)


#: kind -> worker-side handler. review_poller claims only these kinds by default.
HANDLERS: dict[str, Callable[..., dict]] = {
    "case_review": _handle_case_review,
}
