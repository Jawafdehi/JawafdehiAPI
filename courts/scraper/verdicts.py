"""Recover a missing verdict by reading the court's own faisala (judgment).

Hearings normally arrive from the daily cause list, and the deciding hearing is
what carries ``decision_type``. Cases that reached NGM by some other route -- the
register sweep in :mod:`courts.scraper.sweep`, chiefly -- can therefore be marked
decided in ``case_status`` while carrying no hearing with a ``decision_type`` at
all. They count as decided and contribute to no outcome.

The court publishes the full judgment for most of them (see
``scrape_court_orders``), and the judgment states the disposition. This module
holds the deterministic halves -- backlog selection, prompt construction,
response validation, and the hearing row to write -- so they are unit-testable
without a network or a model. The download, the model call and the DB write live
in the ``extract_verdicts`` management command.

WHY A MODEL AND NOT A REGEX: a faisala recites the charge and then the finding,
and a mixed bench convicts some accused while acquitting others, so most
judgments contain BOTH ``ठहर`` and ``सफाई``. Keyword matching picks the wrong one
often enough to matter -- 4 of 6 sampled documents contained both terms. The
distinction between full and partial conviction in particular is only available
from reading the operative section.

EVERY ROW THIS WRITES IS MODEL-DERIVED and is marked as such in
``extra_data`` (:data:`PROVENANCE_KEY`). Downstream consumers that publish
conviction rates must be able to tell these apart from court-scraped verdicts --
see :func:`derived_hearing_filter`.

CALIBRATION (2026-07-31, ``--eval 24`` on Special Court cases whose disposition
the court had already given us, Opus 4.8[1m]):

    prompt v1   21 correct / 3 WRONG / 0 abstain   87.5%
    prompt v2   21 correct / 0 wrong / 3 errored   100.0% on answered

Every one of v1's three errors ran the same way -- ``ठहर`` reported as
``आंशिक ठहर`` -- because v1 defined आंशिक to include "a materially reduced
claim/amount". It does not. The court reserves आंशिक ठहर for the CHARGE standing
only in part: an accused acquitted, or a count dismissed. Differing fines between
convicted accused, a बिगो below what was demanded, or seized money returned as
private property are all still ``ठहर``. v2 states that as an ordered test and
requires the model to NAME the acquitted accused before answering आंशिक.

The remaining 3 are the longest, most-multi-defendant judgments; they exhaust the
output-token budget or time out. That is the intended failure mode -- a skipped
case costs coverage, a wrong one corrupts a published conviction rate -- so
expect roughly one case in eight to be left for a human rather than answered.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from django.db.models import Exists, OuterRef, Q

from courts.case_status import parse_case_status
from courts.models import CourtCase, CourtCaseHearing

#: The court's three dispositions, exactly as ``decision_type`` stores them.
#: This is a closed set -- anything else is a parse failure, not a new category.
FULL = "ठहर"
PARTIAL = "आंशिक ठहर"
ACQUITTAL = "सफाई"
DECISION_TYPES = (FULL, PARTIAL, ACQUITTAL)

#: Marker under ``CourtCaseHearing.extra_data`` identifying a model-derived row.
PROVENANCE_KEY = "verdict_extraction"

#: Returned by the model when the judgment does not settle the disposition.
#: Recorded as a skip, never written -- an abstention is a correct answer.
ABSTAIN = "ABSTAIN"

#: Judgments run 13k-19k characters. The operative finding (``ठहर खण्ड``) and the
#: directions (``तपसिल``) sit at the END, while the bench sits at the head, so a
#: naive truncation drops exactly the part that decides the answer. Keep both
#: ends when a document is too long to send whole.
MAX_CHARS = 24_000
HEAD_CHARS = 6_000

SYSTEM_PROMPT = """You read judgments (फैसला) of Nepal's Special Court and report the disposition.

Return ONLY a JSON object, no prose and no code fence:

{"decision_type": "<one of: ठहर | आंशिक ठहर | सफाई | ABSTAIN>",
 "judges": "<presiding and member judges, newline-separated, as printed>",
 "verdict_date_bs": "<YYYY-MM-DD in Bikram Sambat, or null>",
 "evidence": "<the clause you decided from, quoted verbatim, max 300 chars>",
 "confidence": "<high | medium | low>"}

The three dispositions:
  ठहर        FULL. Every accused is held guilty. The charge stands in full.
  आंशिक ठहर  PARTIAL. The charge stands only IN PART.
  सफाई       ACQUITTAL. Every accused is cleared.

Apply ONE test, in this order:
  1. Is EVERY accused acquitted?                     -> सफाई
  2. Is at least one accused ACQUITTED (सफाई पाउने)
     while at least one other is convicted, OR is a
     charge/count expressly DISMISSED while another
     is upheld?                                      -> आंशिक ठहर
  3. Otherwise (every accused convicted)             -> ठहर

आंशिक ठहर is about WHO and WHICH CHARGE, never about how much. All of the
following are still ठहर, not आंशिक ठहर, as long as no accused is acquitted and
no count is dismissed:
- different fines or sentences for different accused;
- a fine or बिगो smaller than the amount the prosecution demanded;
- seized money partly returned as the accused's own private property;
- conviction under a lesser section than the one charged.

How to read one:
- Decide from the OPERATIVE section -- the ठहर खण्ड and the तपसिल directions near
  the end. The earlier sections recite the charge and the defence; they state
  what was ALLEGED, not what was decided.
- The operative verbs are ठहर्छ / ठहरेको (held) and सफाई पाउने ठहर्छ (acquitted).
- Nearly every judgment contains both the words ठहर and सफाई. Their presence
  proves nothing -- सफाई usually appears only because the defence ASKED for it.
  Only the operative holding counts.
- Before answering आंशिक ठहर, name the accused who was acquitted or the count
  that was dismissed. If you cannot name one, the answer is ठहर.

Answer ABSTAIN if the text is truncated before the operative section, is
unreadable, or is not a Special Court judgment. Do not guess: a wrong verdict is
far worse than an abstention."""


@dataclass(frozen=True)
class VerdictExtraction:
    """A parsed model answer. ``decision_type`` is None when the model abstained."""

    decision_type: str | None
    judges: str | None
    verdict_date_bs: str | None
    evidence: str | None
    confidence: str | None
    raw: str

    @property
    def abstained(self) -> bool:
        return self.decision_type is None


def build_prompt(text: str, case_number: str) -> str:
    """Shape one judgment into the user message.

    Long documents keep their head (the bench) and their tail (the holding)
    rather than the first N characters, which would drop the answer.
    """
    body = " ".join((text or "").split())
    if len(body) > MAX_CHARS:
        keep_tail = MAX_CHARS - HEAD_CHARS
        body = f"{body[:HEAD_CHARS]}\n\n[... middle omitted ...]\n\n{body[-keep_tail:]}"
    return f"Case number: {case_number}\n\nJudgment:\n{body}"


def parse_response(raw: str) -> VerdictExtraction:
    """Validate a model answer into a :class:`VerdictExtraction`.

    Raises ValueError on anything that is not a usable answer, so a malformed
    response is a visible failure rather than a silently skipped case.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty response")

    # Models occasionally wrap JSON in a fence despite the instruction, or
    # prepend a sentence. Take the outermost object.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object in response: {text[:120]!r}")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("response JSON is not an object")

    decision = (data.get("decision_type") or "").strip()
    if decision == ABSTAIN:
        decision = None
    elif decision not in DECISION_TYPES:
        raise ValueError(f"decision_type not one of {DECISION_TYPES}: {decision!r}")

    date_bs = (data.get("verdict_date_bs") or "").strip() or None
    if date_bs and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_bs):
        # A stray format is not worth failing the whole extraction over; the
        # authoritative date comes from case_status anyway.
        date_bs = None

    def _text(key, cap):
        v = data.get(key)
        return " ".join(str(v).split())[:cap] if v else None

    return VerdictExtraction(
        decision_type=decision,
        judges=_text("judges", 500),
        verdict_date_bs=date_bs,
        evidence=_text("evidence", 300),
        confidence=(data.get("confidence") or "").strip().lower() or None,
        raw=text,
    )


def has_deciding_hearing() -> Exists:
    """Subquery: the case already carries a hearing with a real disposition."""
    return Exists(
        CourtCaseHearing.objects.filter(
            case_number=OuterRef("case_number"),
            court_id=OuterRef("court_id"),
            decision_type__in=DECISION_TYPES,
        )
    )


def backlog(court_identifier="special", case_number=None):
    """Cases marked decided, holding an order document, and carrying no verdict.

    Ordered newest-register-first so a truncated run still makes useful
    progress on the years most likely to be cited.
    """
    qs = CourtCase.objects.filter(court_id=court_identifier)
    if case_number:
        return qs.filter(case_number=case_number)
    return (
        qs.filter(extra_data__has_key="court_orders")
        .annotate(decided=has_deciding_hearing())
        .filter(decided=False)
        .exclude(Q(case_status__isnull=True) | Q(case_status=""))
        .order_by("-case_number")
    )


def is_decided(case) -> bool:
    """True when ``case_status`` says a verdict exists (via the shared parser)."""
    return parse_case_status(case.case_status).lifecycle_status == "DECIDED"


def order_urls(case) -> list[str]:
    """Fetchable order-document URLs for a case, RAW first.

    Two storage generations have to be read here, and only one of them is
    self-describing:

    * ``document_sources`` CANONICAL (post-monolith capture) -- ``url`` is a LIST
      of ``{link, role}``.
    * ``document_sources`` LEGACY HYBRID (the 2026-07 rehost) -- ``url`` is a
      SCALAR string and the roled list lives under ``links``.
      ``materials.jsonld.media_objects_from_document_sources`` drops these,
      because it requires ``url`` to be a list.
    * ``extra_data['court_orders']`` holds ABSOLUTE urls on newly captured cases
      but RELATIVE keys (``court-orders/special/<case>.1.doc``) on historical
      ones, so it is only a fallback and only when it is already absolute.
    """
    out: list[str] = []

    def add(link):
        if isinstance(link, str) and link.startswith("http") and link not in out:
            out.append(link)

    raw, other = [], []
    for src in getattr(case, "document_sources", None) or []:
        if not isinstance(src, dict) or src.get("source_type") != "COURT_ORDER":
            continue
        url = src.get("url")
        candidates = []
        if isinstance(url, list):
            candidates = url
        elif isinstance(url, str):
            # Legacy hybrid: the scalar is the primary, `links` carries the roles.
            candidates = [{"link": url, "role": "RAW"}, *(src.get("links") or [])]
        for link in candidates:
            if isinstance(link, dict):
                (raw if (link.get("role") or "").upper() == "RAW" else other).append(link.get("link"))
            elif isinstance(link, str):
                other.append(link)
    for link in (*raw, *other):
        add(link)

    for entry in (case.extra_data or {}).get("court_orders") or []:
        add(entry)
    return out


def derived_hearing_filter() -> Q:
    """Q matching hearing rows this module wrote.

    Exists so consumers can include or exclude model-derived verdicts
    deliberately. Publishing a conviction rate over a mix of court-scraped and
    model-derived rows without saying so would misrepresent the data.
    """
    return Q(**{f"extra_data__{PROVENANCE_KEY}__isnull": False})


def build_hearing(case, extraction: VerdictExtraction, *, order_url, model, now):
    """The unsaved :class:`CourtCaseHearing` for one recovered verdict.

    The verdict DATE comes from ``case_status`` -- that is the court's own field
    and is not in question. Only the disposition is model-derived.
    """
    if extraction.abstained:
        raise ValueError("refusing to build a hearing from an abstention")
    parsed = parse_case_status(case.case_status)
    if not parsed.verdict_date_bs or not parsed.verdict_date_ad:
        raise ValueError(f"no verdict date in case_status: {case.case_status!r}")

    return CourtCaseHearing(
        case_number=case.case_number,
        court_id=case.court_id,
        hearing_date_bs=parsed.verdict_date_bs,
        hearing_date_ad=parsed.verdict_date_ad,
        decision_type=extraction.decision_type,
        judge_names=extraction.judges,
        case_status=case.case_status,
        scraped_at=now,
        extra_data={
            PROVENANCE_KEY: {
                "derived": True,
                "method": "llm_read_of_court_order",
                "model": model,
                "order_url": order_url,
                "confidence": extraction.confidence,
                "evidence": extraction.evidence,
                "model_verdict_date_bs": extraction.verdict_date_bs,
                "extracted_at": now.isoformat(),
            }
        },
    )
