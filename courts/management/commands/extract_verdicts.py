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
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from courts.models import CourtCaseHearing
from courts.scraper.registers import NGM_DB
from courts.scraper.verdicts import (
    DECISION_TYPES,
    PROVENANCE_KEY,
    backlog,
    build_hearing,
    build_prompt,
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
    def _extract_one(self, case, *, tier):
        """Download the order, read it, return (extraction, url, model, chars).

        Raises RuntimeError with a short reason on any unusable case, so the
        caller can count failure kinds instead of swallowing them.
        """
        from casework.convert import extract_markdown
        from llm import invoke, routing

        urls = order_urls(case)
        if not urls:
            raise RuntimeError("no order url on case")

        text = ""
        used = None
        for url in urls:
            try:
                text = extract_markdown(url, timeout=180)
            except Exception as exc:  # noqa: BLE001 - one bad artefact != a dead case
                self.stderr.write(f"    fetch failed {url}: {type(exc).__name__}: {exc}")
                continue
            if text and text.strip():
                used = url
                break
        if not used:
            raise RuntimeError("no order document yielded text")

        model = routing.provider_for_tier(tier).model_for_tier(tier)
        raw = invoke.invoke_text(SYSTEM_PROMPT, build_prompt(text, case.case_number), MAX_TOKENS, tier=tier)
        return parse_response(raw), used, model, len(text)

    # --------------------------------------------------------------------- eval
    def _run_eval(self, court, n, tier, delay):
        """Score the extractor on cases whose disposition the court already gave.

        This is the only evidence that the written verdicts are trustworthy, so
        it deliberately draws from the SAME population (has an order document,
        Special Court judgment) and just happens to know the answer.
        """
        from courts.models import CourtCase

        truth = dict(
            CourtCaseHearing.objects.using(NGM_DB)
            .filter(court_id=court, decision_type__in=DECISION_TYPES)
            .values_list("case_number", "decision_type")
        )
        # Spread the sample across the register rather than taking one era.
        cases = list(
            CourtCase.objects.using(NGM_DB)
            .filter(court_id=court, case_number__in=list(truth), extra_data__has_key="court_orders")
            .order_by("case_number")
        )
        if not cases:
            raise CommandError("no evaluable cases (need an order document + a known verdict)")
        step = max(1, len(cases) // n)
        sample = cases[::step][:n]

        self.stdout.write(f"eval: {len(sample)} cases sampled from {len(cases)} with both an order and a known verdict\n")
        hits = miss = abst = err = 0
        confusion = {}
        for i, case in enumerate(sample, 1):
            want = truth[case.case_number]
            try:
                ex, url, _model, chars = self._extract_one(case, tier=tier)
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
            time.sleep(delay)

        answered = hits + miss
        self.stdout.write("\n=== eval ===")
        self.stdout.write(f"  correct    {hits}")
        self.stdout.write(f"  wrong      {miss}")
        self.stdout.write(f"  abstained  {abst}")
        self.stdout.write(f"  errored    {err}")
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

        if opts.get("eval"):
            return self._run_eval(court, opts["eval"], tier, delay)

        qs = backlog(court_identifier=court, case_number=opts.get("case")).using(NGM_DB)
        cases = list(qs[: opts["limit"]])
        mode = "WRITE" if opts["write"] else "dry-run"
        self.stdout.write(f"{mode}: {len(cases)} case(s) from the {court} backlog\n")

        written = skipped = abstained = failed = 0
        for i, case in enumerate(cases, 1):
            if not is_decided(case):
                skipped += 1
                self.stdout.write(f"  {i:>3} {case.case_number}  skip: case_status is not decided")
                continue
            try:
                ex, url, model, chars = self._extract_one(case, tier=tier)
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

            # Re-check inside the transaction: hearings carry no unique
            # constraint, so a concurrent cause-list scrape (or a second run of
            # this command) would otherwise duplicate the row.
            with transaction.atomic(using=NGM_DB):
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
            time.sleep(delay)

        self.stdout.write(
            f"\n{mode} done: written={written} abstained={abstained} skipped={skipped} failed={failed}"
        )
        if not opts["write"] and written == 0:
            self.stdout.write(f"(dry-run — nothing persisted; rows would carry extra_data['{PROVENANCE_KEY}'])")
        return 0
