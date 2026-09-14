"""Proceeding-stage records built from one court docket, and merged into a case.

Pure: dicts in, dicts out. No API, no Django, no model -- the stage vocabulary
and its server-side rules live in `cases.stages`, which this must not import
because the enricher runs without Django configured.
"""

from courts.case_status import parse_case_status

#: The one stage kind this enricher writes. A trial docket is a first instance.
STAGE_INITIAL = "initial"

#: `case_status` on a hearing row that decides the case.
DECIDING_STATUS = "फैसला"

#: The stage keys this module owns. Everything else on an existing record --
#: `notes`, `body`, `label` -- is a human's and survives a merge untouched.
_OWNED = ("start", "end")


def deciding_hearing(hearings):
    """The hearing that decided the case, or None -- by max date, never position."""
    decided = [h for h in (hearings or ())
               if DECIDING_STATUS in (h.get("case_status") or "")]
    if not decided:
        return None
    return max(decided, key=lambda h: h.get("hearing_date_ad") or "")


def reference_start(record):
    """`YYYY-MM-DD` this docket was registered on, or ""."""
    return str((record.get("detail") or {}).get("registration_date_ad") or "")


def reference_end(record):
    """`YYYY-MM-DD` this docket decided on, or "".

    Deciding hearing first, then the `case_status` paren-date -- 29 of the
    census's 307 cases carry only the second.
    """
    hearing = deciding_hearing(record.get("hearings"))
    if hearing and hearing.get("hearing_date_ad"):
        return str(hearing["hearing_date_ad"])
    parsed = parse_case_status((record.get("detail") or {}).get("case_status"))
    return parsed.verdict_date_ad.isoformat() if parsed.verdict_date_ad else ""


def trial_stage(record):
    """The `initial` stage one trial docket proposes.

    A date the docket does not carry is OMITTED, never written empty: an absent
    `end` is what the model reads as an open proceeding.
    """
    stage = {"stage": STAGE_INITIAL}
    if start := reference_start(record):
        stage["start"] = start
    if end := reference_end(record):
        stage["end"] = end
    stage["courtcase_iri"] = record["iri"]
    return stage


def docket_label(iri):
    """`special/079-CR-0151` from a courtcase IRI, for a report line."""
    parts = str(iri or "").strip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else str(iri or "")


def _adoptable(stages, proposals):
    """Index of an `initial` stage with no IRI, when exactly one trial exists.

    With no trial there is nothing to adopt; with several, nothing says which.
    """
    if len(proposals) != 1:
        return None
    for index, stage in enumerate(stages):
        if stage.get("stage") == STAGE_INITIAL and not stage.get("courtcase_iri"):
            return index
    return None


def _apply(target, proposal, changes, label):
    """Write the proposal's dates onto `target`, reporting each decision.

    A value replaces a value; an absence never deletes one. Blanking a stored
    `end` would reopen a case that reads as concluded.
    """
    for key in _OWNED:
        new, old = proposal.get(key), target.get(key)
        if not new:
            if old:
                changes.append(f"{label}: {key} kept at {old} -- the court "
                               "record carries none")
            continue
        if old == new:
            continue
        changes.append(f"{label}: {key} {old or '(empty)'} -> {new}")
        target[key] = new


def merge_trial_stages(existing, proposals):
    """`(stages, changes)` -- the case's whole stage list with trials applied.

    Never mutates `existing`; the caller PATCHes the returned list wholesale, so
    every record the case already had must come back in it.
    """
    stages = [dict(s) for s in (existing or []) if isinstance(s, dict)]
    changes = []
    if not proposals:
        return stages, changes

    adopt = _adoptable(stages, proposals)
    for proposal in proposals:
        iri = proposal["courtcase_iri"]
        label = docket_label(iri)
        match = next((s for s in stages if s.get("courtcase_iri") == iri), None)
        if match is not None:
            _apply(match, proposal, changes, label)
            continue
        if adopt is not None:
            target = stages[adopt]
            target["courtcase_iri"] = iri
            changes.append(f"{label}: adopted the case's existing "
                           "first-instance stage, which cited no docket")
            _apply(target, proposal, changes, label)
            adopt = None
            continue
        stages.append(dict(proposal))
        dates = ", ".join(f"{k} {proposal[k]}" for k in _OWNED if proposal.get(k))
        changes.append(
            f"{label}: added a first-instance stage ({dates or 'no dates'})")
    return stages, changes
