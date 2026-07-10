"""``crawl_nkp`` — crawl the Nepal Law Journal (NKP) precedent corpus to JSONL.

Thin management-command wrapper over :class:`materials.sourcing.nkp.crawl.
NkpCrawler`. A data-acquisition tool (dev/ops), not a request-path feature — it
writes ``decisions.jsonl`` which ``ingest_nkp_decisions`` then lands as
``precedent`` Materials. Resumable: re-running only fetches what's missing.

Examples::

    manage.py crawl_nkp --out work/nkp/decisions.jsonl                 # whole corpus
    manage.py crawl_nkp --out work/nkp/decisions.jsonl --year 2082     # one BS year
    manage.py crawl_nkp --out work/nkp/decisions.jsonl --year-min 2076 --year-max 2082
    manage.py crawl_nkp --out work/nkp/decisions.jsonl --transport playwright  # F5 fallback
"""

from argparse import Namespace

from django.core.management.base import BaseCommand

from materials.sourcing.nkp.crawl import NkpCrawler


class Command(BaseCommand):
    help = "Crawl nkp.gov.np precedents into a resumable decisions.jsonl."

    def add_arguments(self, parser):
        parser.add_argument("--out", required=True, help="decisions.jsonl path (appended; resume source).")
        parser.add_argument("--year", help="Limit to one BS year.")
        parser.add_argument("--year-min", dest="year_min", type=int, default=None, help="Shard: lowest BS year.")
        parser.add_argument("--year-max", dest="year_max", type=int, default=None, help="Shard: highest BS year.")
        parser.add_argument("--delay", type=float, default=3.0, help="Base seconds between requests (default 3).")
        parser.add_argument(
            "--transport", choices=["requests", "playwright"], default="requests",
            help="HTTP session (default) or a real browser (F5-challenge fallback).",
        )
        parser.add_argument("--headful", action="store_true", help="Show the browser (playwright only).")
        parser.add_argument("--max-decisions", dest="max_decisions", type=int, default=0, help="Stop after N new (0=all).")

    def handle(self, *args, **opts):
        NkpCrawler(
            Namespace(
                out=opts["out"],
                year=opts["year"],
                year_min=opts["year_min"],
                year_max=opts["year_max"],
                delay=opts["delay"],
                transport=opts["transport"],
                headful=opts["headful"],
                max_decisions=opts["max_decisions"],
            )
        ).crawl()
