#!/usr/bin/env python
"""Accused binds and case dates, read from the case's own NGM court record.

Zero LLM calls, zero Django, zero source documents. The court record states
these facts rather than inferring them: a defendant is a defendant because a
charge sheet says so, and a verdict date is a verdict date because the Special
Court's docket says so.

WHAT IT WRITES, in one conditional PATCH per case (`CaseworkApi.patch_case`):

  case_start_date  the earliest registration date across the case's court
                   references, and only when the field is currently empty.
  case_end_date    the latest deciding-hearing date, and only when the field is
                   empty AND every court reference on the case has decided.
  entities         the existing bind list with new `accused` binds appended --
                   a whole-list replace of a list merged in application code,
                   never a delta (`merge_entity_binds`).

MEASURED COVERAGE (2026-08-07, anonymous GET, all 307 cases of the FY078/079
census): 307/307 carry a registration date, 306/307 carry an end date, and
307/307 name at least one defendant (1,343 rows). The rule reproduces the
hand-entered convention -- start matches on 46 of 48 published cases, end on
29 of 29 where a deciding hearing exists.

WHY NOT THE SCORED RESOLVER. `casework.entity_resolver` binds the best candidate
above a threshold. NES holds 162,650 person entities dominated by Election
Commission candidate records, so a scored match can name a namesake as the
accused in a corruption case -- the worst error this platform can make. This
module matches on exact name equality within the `person` prefix, and creates
the entity when there is no unique exact match. The failure mode becomes a
duplicate entity, which is a merge, not a defamation.

WHY IT NEVER WRITES `convicted`. `decision_type` sits on the CASE, not on each
defendant. `ठहर` on a 19-defendant case does not say who, and `आंशिक ठहर` means
some were convicted and some cleared. `सफाई` is a whole-case acquittal, so it
alone is distributed to each defendant -- and only ever corrects an unfairly
plain "Accused" label. `charged` is true by construction everywhere else: every
case in this corpus is a Special Court `-CR-` case, so CIAA filed a charge sheet.

Usage:
    uv run python -m casework.enrich_court_record --dry-run --verbose
"""

import logging

from courts.case_status import parse_case_status

logger = logging.getLogger(__name__)

#: Case states this stage may write to. Matches `enrich_related_entities`.
REQUIRED_WRITE_STATE = "DRAFT"

#: `case_status` on a hearing row that decides the case.
DECIDING_STATUS = "फैसला"

#: The whole-case acquittal. The ONLY disposition distributed to each defendant.
ACQUITTAL = "सफाई"

#: NES prefix and schema.org type a court defendant is created under.
PERSON_PREFIX = "person"
PERSON_TYPE = "Person"

#: A verdict is legal only on an accused bind (the `outcome_only_on_accused`
#: CHECK constraint). Sent explicitly so the claim is visible in the request
#: body rather than implied by the API's omitted-outcome fallback.
CHARGED = "charged"
ACQUITTED = "acquitted"


def deciding_hearing(hearings):
    """The hearing that decided the case, or None.

    Picked by MAX `hearing_date_ad` among rows whose `case_status` names a
    verdict -- never by list position. The hearings endpoint does not sort by
    date: on special/079-CR-0151 the 2081-02-22 verdict is returned BEFORE the
    2081-02-21 order that precedes it.
    """
    decided = [h for h in (hearings or ())
               if DECIDING_STATUS in (h.get("case_status") or "")]
    if not decided:
        return None
    return max(decided, key=lambda h: h.get("hearing_date_ad") or "")


def _reference_end(record):
    """`YYYY-MM-DD` this reference decided on, or "" if it has not.

    Two sources, checked in that order: the deciding hearing row, then the
    `case_status` string (`फैसला (मिती: २०८१/०२/२२)`), which
    `courts.case_status.parse_case_status` already converts BS->AD. Across the
    307-case census the two agreed 277 times out of 277, and 29 cases carry only
    the second -- so the fallback is what those 29 depend on, not a tiebreak.
    """
    hearing = deciding_hearing(record.get("hearings"))
    if hearing and hearing.get("hearing_date_ad"):
        return str(hearing["hearing_date_ad"])
    parsed = parse_case_status((record.get("detail") or {}).get("case_status"))
    return parsed.verdict_date_ad.isoformat() if parsed.verdict_date_ad else ""


def start_date(records):
    """The earliest `registration_date_ad` across every court reference, or "".

    Earliest, not first: a case citing two court references started when the
    first of them was registered.
    """
    dates = [str((r.get("detail") or {}).get("registration_date_ad") or "")
             for r in records]
    return min((d for d in dates if d), default="")


def end_date(records):
    """`(value, reason)` -- when the case ended, or "" and why not.

    A case ends when EVERY court reference on it has been decided. One
    undecided reference means the case is still being heard, and
    `case_end_date` is load-bearing on the public site: the frontend's
    `deriveCaseStatus` reads any non-empty value as "concluded" and changes the
    status chip. Half-decided is not decided.
    """
    if not records:
        return "", "no readable court reference"
    ends = [_reference_end(r) for r in records]
    if not any(ends):
        return "", "no decision on record: the case has not been decided"
    if not all(ends):
        undecided = [f"{r['court']}/{r['number']}"
                     for r, e in zip(records, ends) if not e]
        return "", ("not every court reference has decided (still open: "
                    + ", ".join(undecided) + ")")
    return max(ends), ""
