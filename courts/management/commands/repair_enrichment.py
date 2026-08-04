"""Re-enrich cases that were marked ``enriched`` without ever being enriched.

Two bugs let an empty detail-page parse through :func:`courts.scraper.base.apply_enrichment`:

* ``_Supreme.crawl_detail`` fetched the ``regno`` **search result list** and handed
  it to the detail parser, which found nothing in it — so every Supreme enrichment
  produced an empty ``ParsedEnrichment``.
* The guard was ``core_fields or extra_data or entities``, and the supreme/district/
  high parsers always emit their ``enrichment_hearings``/``enrichment_timeline``
  keys — so ``extra_data`` was truthy even for a page that identified nothing.

The write went ahead and set ``status = "enriched"``. ``crawl._enrich_pending``
excludes that status, so those cases are now permanently skipped: the fix alone
does not reach them. This command does.

It targets rows marked enriched that carry **no** enrichment evidence — no
``registration_number``, no ``hearing_count``, no hearings JSON — and simply
re-runs the (now correct) enrichment over them. Nothing is reset first: a case
that enriches successfully overwrites its own state, and one that doesn't is left
exactly as it was rather than downgraded.

    manage.py repair_enrichment --court supreme              # dry run: count + sample
    manage.py repair_enrichment --court supreme --apply

This writes to the live mirror and takes ~2 fetches per case, so run it as a Job
rather than ``kubectl exec`` — a rollout SIGKILLs exec'd processes mid-run.
"""

import time

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from courts.models import CourtCase
from courts.scraper import base, registry
from courts.scraper.fetch import Fetcher

#: Marked enriched, yet holding nothing an enrichment would have written.
DAMAGED = (
    Q(status="enriched")
    & Q(registration_number__isnull=True)
    & Q(hearing_count__isnull=True)
    & (Q(extra_data__enrichment_hearings=[]) | Q(extra_data__enrichment_hearings__isnull=True))
)


class Command(BaseCommand):
    help = "Re-enrich cases falsely marked enriched by the empty-parse bug."

    def add_arguments(self, parser):
        parser.add_argument("--court", required=True,
                            help="registry key: special | district | high | supreme")
        parser.add_argument("--court-id", default=None,
                            help="restrict to one leaf court (default: every court in the tier)")
        parser.add_argument("--apply", action="store_true",
                            help="actually re-fetch and write (default: dry run)")
        parser.add_argument("--limit", type=int, default=None,
                            help="cap the number of cases repaired this run")
        parser.add_argument("--delay", type=float, default=1.0,
                            help="seconds between cases (default 1.0)")

    def handle(self, *args, **o):
        try:
            keys = registry.resolve(o["court"])
        except KeyError as exc:
            raise CommandError(str(exc)) from exc
        if len(keys) != 1:
            raise CommandError("--court must name a single tier, not 'all'.")
        module = registry.REGISTRY[keys[0]]
        if not hasattr(module, "crawl_detail"):
            raise CommandError(f"{keys[0]} has no crawl_detail; nothing to repair.")

        damaged = CourtCase.objects.using(base.NGM_DB).filter(DAMAGED)
        if o["court_id"]:
            damaged = damaged.filter(court_id=o["court_id"])
        else:
            damaged = damaged.filter(court_id__in=module.court_ids(None))
        targets = list(damaged.values_list("court_id", "case_number")[: o["limit"]])

        self.stdout.write(f"{len(targets)} case(s) marked enriched with no enrichment.")
        if not o["apply"]:
            for court_id, case_number in targets[:10]:
                self.stdout.write(f"  {court_id}/{case_number}")
            self.stdout.write(self.style.WARNING("dry run — pass --apply to repair."))
            return

        fetch = Fetcher()
        repaired = failed = 0
        for court_id, case_number in targets:
            try:
                enrichment = module.crawl_detail(fetch, court_id, case_number)
            except Exception as exc:  # noqa: BLE001 - a portal flake must not abandon the rest
                failed += 1
                self.stderr.write(f"  {court_id}/{case_number}: {exc}")
                time.sleep(o["delay"])
                continue
            if enrichment is not None and base.apply_enrichment(
                court_id, case_number, enrichment
            ):
                repaired += 1
            else:
                # Still nothing on the portal. Left untouched rather than
                # downgraded — the row is no worse off than before this ran.
                failed += 1
            time.sleep(o["delay"])
        self.stdout.write(f"repaired {repaired}, still unenriched {failed}.")
