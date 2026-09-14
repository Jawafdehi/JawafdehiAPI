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
    start, end = reference_start(record), reference_end(record)
    if start:
        stage["start"] = start
    # The impossible pair is dropped at the merge, not here, so the caller can
    # report WHICH date was refused against the start it lost to.
    if end:
        stage["end"] = end
    stage["courtcase_iri"] = record["iri"]
    return stage


def docket_label(iri):
    """`special/079-CR-0151` from a courtcase IRI, for a report line."""
    parts = str(iri or "").strip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else str(iri or "")


def _orderable(start, end):
    """Whether `end` may sit on a stage starting at `start`.

    Equality is fine: the charge sheet can close one stage and open the next on
    the same day. A missing half is not a conflict.
    """
    return not (start and end) or str(end) >= str(start)


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


def _refused_keys(target, proposal):
    """The proposal dates to drop, as a tuple -- judged before any of them lands.

    NGM carries inverted pairs: special/076-cr-0294 registers 2020-02-25 and
    records a verdict on 2020-01-17. `validate_stages` refuses `end < start`,
    and the enricher sends the stage document and the accused binds as ONE
    conditional request -- so an inverted pair 422s the whole case and takes
    the binds down with it, after the new NES entities have already been POSTed.

    Both dates are judged together because either one can be the inverting
    half: a `start` written alone on a stage that already carries an `end`
    breaks the rule exactly as an `end` does against a stored `start`. Only a
    date this merge is actually CHANGING can be dropped -- refusing to rewrite
    a value that is already stored would not remove it.

    The court record's date is the one that loses; when it carries both halves
    the `end` goes, keeping the registration date, which is migration 0068's
    decision.
    """
    changing = [k for k in _OWNED
                if proposal.get(k) and proposal[k] != target.get(k)]
    if not changing:
        return ()
    final = {k: (proposal.get(k) or target.get(k)) for k in _OWNED}
    if _orderable(final["start"], final["end"]):
        return ()
    first = "end" if "end" in changing else "start"
    # Dropping one half leaves the OTHER half at whatever the target already
    # stores -- which is not the pair judged above, and can invert in its own
    # right. Judge what will actually be written; if the court record and the
    # stored dates cannot be reconciled, write neither and name both.
    kept = {k: (target.get(k) if k == first else final[k]) for k in _OWNED}
    if _orderable(kept["start"], kept["end"]):
        return (first,)
    return _OWNED


def _apply(target, proposal, changes, label, *, fresh=False):
    """Write the proposal's dates onto `target`, reporting each decision.

    A value replaces a value; an absence never deletes one. Blanking a stored
    `end` would reopen a case that reads as concluded.

    `fresh` suppresses the ordinary per-date lines for a stage being created --
    the caller reports its dates once, as one "added" line. The refusal below
    still reports, because a dropped date is not visible in that summary.
    """
    refused = _refused_keys(target, proposal)
    for key in _OWNED:
        new, old = proposal.get(key), target.get(key)
        if not new:
            if old and not fresh:
                changes.append(f"{label}: {key} kept at {old} -- the court "
                               "record carries none")
            continue
        if old == new:
            continue
        if key in refused:
            other = "end" if key == "start" else "start"
            # Name the value that SURVIVES, not the one proposed: when both
            # halves are refused the surviving other half is the stored one.
            surviving = (target.get(other) if other in refused
                         else proposal.get(other) or target.get(other))
            changes.append(
                f"{label}: {key} {new} DROPPED -- it is "
                f"{'after' if key == 'start' else 'before'} the stage {other} "
                f"{surviving}, which the stage schema refuses")
            continue
        if not fresh:
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
        added = {"stage": proposal["stage"], "courtcase_iri": iri}
        _apply(added, proposal, changes, label, fresh=True)
        stages.append(added)
        dates = ", ".join(f"{k} {added[k]}" for k in _OWNED if added.get(k))
        changes.append(
            f"{label}: added a first-instance stage ({dates or 'no dates'})")
    return stages, changes
