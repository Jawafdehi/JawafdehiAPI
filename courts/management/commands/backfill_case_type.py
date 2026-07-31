"""Replace a coarse ``case_type`` with the real charge, from the court's detail page.

Supreme's pages carry two labels — ``मुद्दाको किसिम`` (the class: फौजदारी / देवानी)
and ``मुद्दा`` (the charge: घुस, सरकारी सम्पत्ति हिनामिना, लिखत वदर …). The legacy
import stored the class, so **46,373 of 103,934 supreme rows say only "criminal" or
"civil"**. The parser bug that did the same thing on the live path is fixed, but the
fix cannot reach rows already written: ``case_type`` is deliberately outside
``_ENRICH_COLUMNS`` (the cause list owns it), so re-enrichment leaves them alone.

Why it matters beyond tidiness: ``courts.search_visibility`` maps ``case_type`` to a
canonical code to decide public-index membership, and a class can only ever map to
OTHER_CRIMINAL. A Supreme bribery case labelled फौजदारी is indistinguishable from a
homicide and is evicted from ``ngm-courtcases`` with it. Measured yield on a
stratified sample of 12: **12 improved, 0 unchanged**, three of them corruption.

Deliberately narrow. It rewrites ``case_type``/``case_subject`` and records the class
in ``extra_data.case_class`` — nothing else. It does NOT call ``apply_enrichment``,
so parties are never passed through ``_replace_entities`` and no ``nes_id`` link is
put at risk, and ``status`` is untouched.

Resumable by construction: a repaired row no longer matches the filter.

    manage.py backfill_case_type --court supreme --series CR
    manage.py backfill_case_type --court supreme --series CR --apply
"""

import time

from django.core.management.base import BaseCommand, CommandError

from courts.models import CourtCase
from courts.scraper import base, registry

#: Class labels that are not charges. A row holding one of these tells you only
#: which half of the court's docket it is on.
COARSE_TYPES = ["फौजदारी", "देवानी"]


class Command(BaseCommand):
    help = "Re-fetch detail pages to replace a coarse case_type with the real charge."

    def add_arguments(self, parser):
        parser.add_argument("--court", required=True, help="registry key, e.g. supreme")
        parser.add_argument("--court-id", default=None, help="restrict to one leaf court")
        parser.add_argument("--series", default=None,
                            help="restrict to a register series (e.g. CR). Worth doing: "
                                 "CR is the criminal register and carries the corruption "
                                 "cases, RI is 32k writ/revision rows")
        parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
        parser.add_argument("--limit", type=int, default=None, help="cap rows this run")
        parser.add_argument("--delay", type=float, default=3.0, help="seconds between fetches")

    def handle(self, *args, **o):
        try:
            keys = registry.resolve(o["court"])
        except KeyError as exc:
            raise CommandError(str(exc)) from exc
        if len(keys) != 1:
            raise CommandError("--court must name a single tier.")
        module = registry.REGISTRY[keys[0]]
        if not hasattr(module, "crawl_detail"):
            raise CommandError(f"{keys[0]} has no crawl_detail.")

        qs = CourtCase.objects.using(base.NGM_DB).filter(case_type__in=COARSE_TYPES)
        qs = qs.filter(court_id=o["court_id"]) if o["court_id"] else qs.filter(
            court_id__in=module.court_ids(None)
        )
        if o["series"]:
            qs = qs.filter(case_number__contains=f"-{o['series'].upper()}-")
        targets = list(qs.values_list("court_id", "case_number")[: o["limit"]])

        self.stdout.write(f"{len(targets)} row(s) holding only a class, not a charge.")
        if not o["apply"]:
            for court_id, case_number in targets[:10]:
                self.stdout.write(f"  {court_id}/{case_number}")
            self.stdout.write(self.style.WARNING("dry run — pass --apply to rewrite."))
            return

        from courts.scraper.fetch import Fetcher

        fetch = Fetcher()
        fixed = unchanged = failed = 0
        for court_id, case_number in targets:
            try:
                enrichment = module.crawl_detail(fetch, court_id, case_number)
            except Exception as exc:  # a flake must not abandon the rest of the run
                failed += 1
                self.stderr.write(f"  {court_id}/{case_number}: {exc}")
                time.sleep(o["delay"])
                continue
            charge = (enrichment.core_fields or {}).get("case_type") if enrichment else None
            if not charge or charge in COARSE_TYPES:
                # The page has no finer label either — leave the row exactly as it
                # is rather than rewriting it with the value it already holds.
                unchanged += 1
                time.sleep(o["delay"])
                continue

            case = CourtCase.objects.using(base.NGM_DB).filter(
                court_id=court_id, case_number=case_number
            ).first()
            if case is None:
                unchanged += 1
                time.sleep(o["delay"])
                continue
            case.case_type = charge[:200]
            case.case_subject = charge
            klass = (enrichment.extra_data or {}).get("case_class")
            if klass:
                case.extra_data = {**(case.extra_data or {}), "case_class": klass}
            # Narrow save: never touch status, parties or hearings from here.
            case.save(using=base.NGM_DB,
                      update_fields=["case_type", "case_subject", "extra_data"])
            fixed += 1
            time.sleep(o["delay"])
        self.stdout.write(f"rewrote {fixed}, left alone {unchanged}, failed {failed}.")
