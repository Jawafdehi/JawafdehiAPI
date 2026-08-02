"""Recover missing verdicts by reading the court's published faisala.

Cases that reached the mirror without ever appearing on a daily cause list can be
marked decided in ``case_status`` while carrying no hearing with a
``decision_type``. They count as decided and contribute to no outcome. The court
publishes the judgment for most of them; this command reads it and writes the
missing deciding hearing.

Dry-run by default -- ``--write`` persists.

    # score the extractor against verdicts the court already told us
    manage.py extract_verdicts --court special --eval 60

    # see what would be written
    manage.py extract_verdicts --court special --limit 10

    # write
    manage.py extract_verdicts --court special --write

Every row written is model-derived and marked as such under
``extra_data['verdict_extraction']``.

A case whose first attempt runs out of output tokens is retried once at
``ESCALATED_MAX_TOKENS``; nothing else is retried, and a case that exhausts the
escalated budget too stays a loud failure. The run reports ``escalated=N`` so
the hard tail is visible rather than blended into the success count.
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from courts.models import CourtCase, CourtCaseHearing
from courts.scraper.registers import NGM_DB
from courts.scraper.verdicts import (
    DECISION_TYPES,
    PROVENANCE_KEY,
    backlog,
    build_hearing,
    build_prompt,
    court_coded_verdicts,
    has_order,
    is_decided,
    order_urls,
    parse_response,
    SYSTEM_PROMPT,
)

#: Politeness gap between document downloads, matching the order scraper.
DEFAULT_DELAY = 1.0
#: The answer is a small JSON object, but the budget must also cover the model's
#: reasoning: on the CLI provider this becomes CLAUDE_CODE_MAX_OUTPUT_TOKENS, and
#: a 1200 budget failed 3 of 24 eval cases outright (rc=1 after spending ~4800
#: output tokens). Those failures are safe -- the case is skipped, never guessed
#: at -- but they are lost coverage, so give reasoning real headroom.
MAX_TOKENS = 8000
#: Second-attempt budget, used ONLY after a first attempt died of exhaustion.
#: The 2026-07-31 production run lost 3 of 84 cases this way, all of them long
#: multi-defendant judgments where the reasoning alone outran 8000 tokens.
ESCALATED_MAX_TOKENS = 32000
#: How exhaustion presents. ``claude -p`` does not report "out of output tokens":
#: the assistant turn simply ends unfinished, the CLI wants another turn to
#: continue, ``--max-turns 1`` denies it, and the run aborts as
#: ``error_max_turns`` -- "Reached maximum number of turns (1)". So the turn
#: limit is the messenger and the token budget is the cause, which is why
#: escalation raises the budget rather than the turn count.
_EXHAUSTED = ("error_max_turns", "maximum number of turns", "max_tokens")


def _is_exhaustion(exc):
    """True if this failure looks like the model ran out of room, not out of luck.

    Deliberately narrow. A convert failure, a missing document, an auth 403 or a
    malformed response must NOT be retried at 4x the budget -- that would just
    spend four times as much to fail the same way.
    """
    msg = str(exc).lower()
    return any(s.lower() in msg for s in _EXHAUSTED)


class Command(BaseCommand):
    help = "Read court orders to recover missing verdicts (dry-run unless --write)."

    def add_arguments(self, parser):
        parser.add_argument("--court", default="special", help="court_identifier (default: special)")
        parser.add_argument("--case", help="a single case number, for smoke-testing")
        parser.add_argument("--limit", type=int, default=25, help="max cases (default: 25)")
        parser.add_argument("--write", action="store_true", help="persist; otherwise dry-run")
        parser.add_argument(
            "--eval", type=int, metavar="N",
            help="score against N cases whose verdict the court already gave us; never writes",
        )
        parser.add_argument("--tier", default="premium", choices=("premium", "cheap"))
        parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    # ------------------------------------------------------------------ helpers
    def _gap(self, delay):
        """Wait out the politeness gap, then count this download.

        Called immediately before each download, because the download is the only
        thing here that touches the court. Putting the wait on the WRITE instead
        let a dry run -- which fetches every bit as much -- hammer the site at
        full speed, and putting it at the end of the loop let a run of failures
        do the same by skipping it on ``continue``.
        """
        if self._downloads:
            time.sleep(delay)
        self._downloads += 1

    def _extract(self, case, *, tier):
        """:meth:`_extract_one`, retried once at a larger budget if it ran out.

        Escalation happens ONLY after a failure, and only an exhaustion-shaped
        one. That ordering is load-bearing: every case that succeeds does so on
        exactly the parameters the ``--eval`` run scored, so the measured
        accuracy still describes the rows being written. A first attempt at the
        bigger budget would be cheaper to write and would quietly invalidate
        that -- it can only turn a failure into an answer, never change an
        answer already given.

        Returns ``(extraction, url, model, chars, escalated)``.
        """
        try:
            return (*self._extract_one(case, tier=tier, max_tokens=MAX_TOKENS), False)
        except Exception as exc:  # noqa: BLE001
            if not _is_exhaustion(exc):
                raise
            self.stdout.write(
                f"        budget exhausted at {MAX_TOKENS} tokens; retrying at {ESCALATED_MAX_TOKENS}"
            )
            return (*self._extract_one(case, tier=tier, max_tokens=ESCALATED_MAX_TOKENS), True)

    def _extract_one(self, case, *, tier, max_tokens=MAX_TOKENS):
        """Download the order, read it, return (extraction, url, model, chars).

        Raises RuntimeError with a short reason on any unusable case, so the
        caller can count failure kinds instead of swallowing them.
        """
        from llm import invoke, routing
        from review.converter import convert_all

        urls = order_urls(case)
        if not urls:
            raise RuntimeError("no order url on case")

        text = ""
        used = None
        for url in urls:
            # review.converter is the in-repo converter the material_convert
            # worker uses (see materials/job_handlers.py): likhit/MarkItDown,
            # Devanagari OCR at a safe DPI, on-disk cache, and a hard per-source
            # wall-clock timeout so one stalled scan can't hang the whole run.
            # It reports failure in the result dict rather than raising, so one
            # bad artefact costs a URL, not the case.
            (res,) = convert_all([{"url": [url]}])
            if res.get("conversion_status") == "error":
                self.stderr.write(f"    convert failed {url}: {res.get('conversion_note')}")
                continue
            text = res.get("markdown") or ""
            if text.strip():
                used = url
                break
        if not used:
            raise RuntimeError("no order document yielded text")

        model = routing.provider_for_tier(tier).model_for_tier(tier)
        raw = invoke.invoke_text(SYSTEM_PROMPT, build_prompt(text, case.case_number), max_tokens, tier=tier)
        return parse_response(raw), used, model, len(text)

    # --------------------------------------------------------------------- eval
    def _run_eval(self, court, n, tier, delay):
        """Score the extractor on cases whose disposition the court already gave.

        This is the only evidence that the written verdicts are trustworthy, so
        it deliberately draws from the SAME population (has an order document,
        Special Court judgment) and just happens to know the answer.
        """
        truth = dict(court_coded_verdicts(court).using(NGM_DB))
        # Same order-document predicate the writer uses, so the score describes
        # the population that would actually be written. Spread across the
        # register rather than taking one era.
        cases = list(
            CourtCase.objects.using(NGM_DB)
            .filter(has_order(), court_id=court, case_number__in=list(truth))
            .order_by("case_number")
        )
        if not cases:
            raise CommandError("no evaluable cases (need an order document + a known verdict)")
        step = max(1, len(cases) // n)
        sample = cases[::step][:n]

        self.stdout.write(f"eval: {len(sample)} cases sampled from {len(cases)} with both an order and a known verdict\n")
        hits = miss = abst = err = esc = 0
        confusion = {}
        for i, case in enumerate(sample, 1):
            want = truth[case.case_number]
            self._gap(delay)
            try:
                # Escalate here too, or the score stops describing the write
                # path: eval would report as errors the very cases --write now
                # recovers, understating coverage on exactly the hard tail.
                ex, url, _model, chars, escalated = self._extract(case, tier=tier)
                esc += escalated
            except Exception as exc:  # noqa: BLE001
                err += 1
                self.stdout.write(f"  {i:>3}/{len(sample)} {case.case_number}  ERROR {exc}")
                continue
            if ex.abstained:
                abst += 1
                verdict = "ABSTAIN"
            elif ex.decision_type == want:
                hits += 1
                verdict = "ok"
            else:
                miss += 1
                verdict = "WRONG"
                confusion[(want, ex.decision_type)] = confusion.get((want, ex.decision_type), 0) + 1
            self.stdout.write(
                f"  {i:>3}/{len(sample)} {case.case_number}  want={want:<10} got={ex.decision_type or 'ABSTAIN':<10}"
                f" conf={ex.confidence or '-':<6} {chars:>6}c  {verdict}"
            )
            if ex.abstained or verdict == "WRONG":
                self.stdout.write(f"        evidence: {(ex.evidence or '')[:150]}")

        answered = hits + miss
        self.stdout.write("\n=== eval ===")
        self.stdout.write(f"  correct    {hits}")
        self.stdout.write(f"  wrong      {miss}")
        self.stdout.write(f"  abstained  {abst}")
        self.stdout.write(f"  errored    {err}")
        self.stdout.write(f"  escalated  {esc}  (recovered at {ESCALATED_MAX_TOKENS} tokens)")
        if answered:
            self.stdout.write(f"  accuracy on answered: {hits / answered * 100:.1f}%  (n={answered})")
        if confusion:
            self.stdout.write("  confusion (want -> got):")
            for (w, g), c in sorted(confusion.items(), key=lambda kv: -kv[1]):
                self.stdout.write(f"    {w} -> {g}: {c}")
        return 0

    # ------------------------------------------------------------------- handle
    def handle(self, *args, **opts):
        court, tier, delay = opts["court"], opts["tier"], opts["delay"]
        self._downloads = 0

        if opts.get("eval"):
            return self._run_eval(court, opts["eval"], tier, delay)

        qs = backlog(court_identifier=court, case_number=opts.get("case")).using(NGM_DB)
        cases = list(qs[: opts["limit"]])
        mode = "WRITE" if opts["write"] else "dry-run"
        self.stdout.write(f"{mode}: {len(cases)} case(s) from the {court} backlog\n")

        written = skipped = abstained = failed = escalated_n = 0
        for i, case in enumerate(cases, 1):
            if not is_decided(case):
                skipped += 1
                self.stdout.write(f"  {i:>3} {case.case_number}  skip: case_status is not decided")
                continue
            self._gap(delay)
            try:
                ex, url, model, chars, escalated = self._extract(case, tier=tier)
                escalated_n += escalated
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.stdout.write(f"  {i:>3} {case.case_number}  FAILED {exc}")
                continue

            if ex.abstained:
                abstained += 1
                self.stdout.write(f"  {i:>3} {case.case_number}  ABSTAIN ({chars}c) — left alone")
                continue

            now = timezone.now()
            try:
                hearing = build_hearing(case, ex, order_url=url, model=model, now=now)
            except ValueError as exc:
                failed += 1
                self.stdout.write(f"  {i:>3} {case.case_number}  FAILED {exc}")
                continue

            self.stdout.write(
                f"  {i:>3} {case.case_number}  {ex.decision_type:<10} conf={ex.confidence or '-':<6}"
                f" date={hearing.hearing_date_bs} judges={(ex.judges or '').splitlines()[:1]}"
            )
            if not opts["write"]:
                continue

            # Lock the CASE row and re-check under it. Hearings carry no unique
            # constraint -- and can't easily: 500k+ (case, court) pairs already
            # hold more than one row with a non-null decision_type, legitimately,
            # from district-court cause lists. Locking the parent gives the one
            # guarantee that is actually needed here, that two runs of this
            # command (a retried Job, an overlapping one) cannot both decide the
            # same case. A concurrent cause-list scrape does not take this lock,
            # so that window stays open; it is narrow and the duplicate would be
            # a benign repeat of the same disposition. --write therefore needs
            # Postgres; sqlite has no SELECT FOR UPDATE and raises here.
            with transaction.atomic(using=NGM_DB):
                (
                    CourtCase.objects.using(NGM_DB)
                    .select_for_update()
                    .filter(pk=case.pk)
                    .values_list("pk", flat=True)
                    .first()
                )
                exists = (
                    CourtCaseHearing.objects.using(NGM_DB)
                    .filter(
                        case_number=case.case_number,
                        court_id=case.court_id,
                        decision_type__in=DECISION_TYPES,
                    )
                    .exists()
                )
                if exists:
                    skipped += 1
                    self.stdout.write("        skip: a deciding hearing appeared since selection")
                    continue
                hearing.save(using=NGM_DB)
                written += 1

        self.stdout.write(
            f"\n{mode} done: written={written} abstained={abstained} skipped={skipped} failed={failed}"
            f" escalated={escalated_n}"
        )
        if not opts["write"] and written == 0:
            self.stdout.write(f"(dry-run — nothing persisted; rows would carry extra_data['{PROVENANCE_KEY}'])")
        return 0
