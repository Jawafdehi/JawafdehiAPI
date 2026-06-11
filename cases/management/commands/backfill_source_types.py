"""Backfill NULL source_type values for DocumentSource records.

11 deterministic rules, priority order, first match wins. No LLM.

Usage::

    python manage.py backfill_source_types --dry-run
    python manage.py backfill_source_types --dry-run --verbose
    python manage.py backfill_source_types --limit 100
    python manage.py backfill_source_types --source-id source:20260601:abc12345
    python manage.py backfill_source_types --allow-production

CLI flags::

    --dry-run              classify but don't save
    --limit N              process max N sources
    --source-id S          classify a single source by source_id
    --verbose              detailed per-source logging
    --allow-production     required when DEBUG=False
"""

from __future__ import annotations

import re
import urllib.parse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from cases.models import DocumentSource, SourceType

# ── CIAA press release URL pattern ──────────────────────────────────────
CIAA_PRESS_RELEASE_RE = re.compile(r"https://ciaa\.gov\.np/pressrelease/\d+")

# ── NGM court-order domain ──────────────────────────────────────────────
NGM_COURT_ORDER_DOMAIN = "ngm-store.jawafdehi.org"

# ── Media/news domains ──────────────────────────────────────────────────
MEDIA_DOMAINS = frozenset(
    {
        "setopati.com",
        "ekantipur.com",
        "onlinekhabar.com",
        "himalayantimes.com",
        "therisingnepal.org.np",
        "nepalitimes.com",
        "kathmandupost.com",
        "annapurnapost.com",
        "ratopati.com",
        "bbc.com",
        "bbc.co.uk",
    }
)

# ── Investigative-report domains ────────────────────────────────────────
INVESTIGATIVE_DOMAINS = frozenset(
    {
        "investigative.nepal",
        "investigativedaily.com",  # placeholder — extend as real data surfaces
    }
)

# ── Social media domains ────────────────────────────────────────────────
SOCIAL_DOMAINS = frozenset(
    {
        "facebook.com",
        "fb.com",
        "x.com",
        "twitter.com",
        "instagram.com",
        "youtube.com",
        "tiktok.com",
    }
)

# ── Legislative/policy domains ──────────────────────────────────────────
LEGISLATIVE_DOMAINS = frozenset(
    {
        "lawcommission.gov.np",
        "parliament.gov.np",
        "moljpa.gov.np",
    }
)

# ── Financial/forensic keywords in title/description ────────────────────
FINANCIAL_KEYWORDS = (
    "audit",
    "audit report",
    "financial",
    "forensic",
    "bank statement",
    "transaction",
    "money trail",
)
FINANCIAL_NEPALI_KEYWORDS = (
    "लेखापरीक्षण",
    "अडिट",
    "वित्तीय",
    "फरेन्सिक",
    "बैंक स्टेटमेन्ट",
    "कारोबार",
    "मनी ट्रेल",
)

# ── CIAA procedural keywords (title/description) ────────────────────────
CIAA_PROCEDURAL_KEYWORDS = (
    "arrest",
    "arrested",
    "remand",
    "custody",
    "bail",
    "charge-sheet",
)
CIAA_PROCEDURAL_NEPALI_KEYWORDS = (
    "पक्राउ",
    "हिरासत",
    "थुनुवा",
    "धरपकड",
    "नियन्त्रण",
)

# ── Investigative report keywords ───────────────────────────────────────
INVESTIGATIVE_KEYWORDS = (
    "investigation report",
    "probe report",
    "special report",
    "sting",
    "expose",
)
INVESTIGATIVE_NEPALI_KEYWORDS = (
    "अनुसन्धान प्रतिवेदन",
    "विशेष प्रतिवेदन",
    "पर्दाफास",
    "रंगेहात",
)

# ── Public-complaint keywords ───────────────────────────────────────────
PUBLIC_COMPLAINT_KEYWORDS = (
    "complaint",
    "whistleblower",
    "tip-off",
    "petition",
)
PUBLIC_COMPLAINT_NEPALI_KEYWORDS = (
    "उजुरी",
    "गुनासो",
    "सूचना",
    "निवेदन",
)

# ── Internal-corporate keywords ─────────────────────────────────────────
INTERNAL_CORPORATE_KEYWORDS = (
    "internal memo",
    "email",
    "board meeting",
    "minutes of meeting",
    "agreement",
    "contract",
)
INTERNAL_CORPORATE_NEPALI_KEYWORDS = (
    "आन्तरिक ज्ञापन",
    "इमेल",
    "बोर्ड बैठक",
    "सम्झौता",
    "करार",
)

# ── Legislative/policy keywords ─────────────────────────────────────────
LEGISLATIVE_KEYWORDS = (
    "bill",
    "policy",
    "ordinance",
    "regulation",
    "act",
    "law commission",
    "nepal gazette",
)
LEGISLATIVE_NEPALI_KEYWORDS = (
    "विधेयक",
    "नीति",
    "अध्यादेश",
    "नियमावली",
    "ऐन",
    "कानून आयोग",
    "नेपाल राजपत्र",
)


def _any_keyword(corpus: str, *keyword_tuples: tuple[str, ...]) -> bool:
    """True if any keyword from any tuple is found in *corpus*."""
    for tup in keyword_tuples:
        for kw in tup:
            if kw in corpus:
                return True
    return False


def _any_url_domain(domains: frozenset[str], urls: list[str]) -> bool:
    """True if any URL matches any of the given *domains*."""
    return any(_domain_in_url(d, u) for d in domains for u in urls)


def _classify_source_type(source: DocumentSource) -> SourceType | None:
    """Classify a single source using 11 priority-ordered deterministic rules.

    Returns a SourceType value or None if no rule matches.
    """
    title = (source.title or "").strip()
    desc = (source.description or "").strip()
    urls = source.url_links if hasattr(source, "url_links") else (source.url or [])
    urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]

    corpus = f"{title} {desc}".lower()

    # Rule 1: CIAA Press Release → OFFICIAL_GOVERNMENT
    if any(CIAA_PRESS_RELEASE_RE.search(u) for u in urls):
        return SourceType.OFFICIAL_GOVERNMENT

    # Rule 2: NGM Court Orders → LEGAL_COURT_ORDER
    if any(NGM_COURT_ORDER_DOMAIN in u for u in urls):
        return SourceType.LEGAL_COURT_ORDER

    # Rule 3: CIAA Procedural → LEGAL_PROCEDURAL
    if _any_keyword(corpus, CIAA_PROCEDURAL_KEYWORDS, CIAA_PROCEDURAL_NEPALI_KEYWORDS):
        return SourceType.LEGAL_PROCEDURAL

    # Rule 4: Financial/Forensic → FINANCIAL_FORENSIC
    if _any_keyword(corpus, FINANCIAL_KEYWORDS, FINANCIAL_NEPALI_KEYWORDS):
        return SourceType.FINANCIAL_FORENSIC

    # Rule 5: Media/News → MEDIA_NEWS
    if _any_url_domain(MEDIA_DOMAINS, urls):
        return SourceType.MEDIA_NEWS

    # Rule 6: Investigative Reports → INVESTIGATIVE_REPORT
    if _any_url_domain(INVESTIGATIVE_DOMAINS, urls):
        return SourceType.INVESTIGATIVE_REPORT
    if _any_keyword(corpus, INVESTIGATIVE_KEYWORDS, INVESTIGATIVE_NEPALI_KEYWORDS):
        return SourceType.INVESTIGATIVE_REPORT

    # Rule 7: Public Complaint → PUBLIC_COMPLAINT
    if _any_keyword(
        corpus, PUBLIC_COMPLAINT_KEYWORDS, PUBLIC_COMPLAINT_NEPALI_KEYWORDS
    ):
        return SourceType.PUBLIC_COMPLAINT

    # Rule 8: Legislative/Policy → LEGISLATIVE_DOC
    if _any_url_domain(LEGISLATIVE_DOMAINS, urls):
        return SourceType.LEGISLATIVE_DOC
    if _any_keyword(corpus, LEGISLATIVE_KEYWORDS, LEGISLATIVE_NEPALI_KEYWORDS):
        return SourceType.LEGISLATIVE_DOC

    # Rule 9: Social Media → SOCIAL_MEDIA
    if _any_url_domain(SOCIAL_DOMAINS, urls):
        return SourceType.SOCIAL_MEDIA

    # Rule 10: Internal Corporate → INTERNAL_CORPORATE
    if _any_keyword(
        corpus, INTERNAL_CORPORATE_KEYWORDS, INTERNAL_CORPORATE_NEPALI_KEYWORDS
    ):
        return SourceType.INTERNAL_CORPORATE

    # Rule 11: Fallback → OTHER_VISUAL
    return SourceType.OTHER_VISUAL


def _domain_in_url(domain: str, url: str) -> bool:
    """Check if *domain* appears in the hostname of *url*."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        if not host:
            # Fallback for URLs without scheme, e.g. "example.com/foo"
            host = parsed.path.split("/")[0].split(":")[0].lower()
        else:
            host = host.lower()
        return host == domain or host.endswith(f".{domain}")
    except Exception:  # noqa: BLE001
        return False


class Command(BaseCommand):
    help = (
        "Backfill NULL source_type on DocumentSource records using 11 "
        "deterministic keyword/domain rules. No LLM."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Classify but do not save.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Max sources to process (0 = unlimited).",
        )
        parser.add_argument(
            "--source-id",
            type=str,
            default=None,
            help="Classify a single source by source_id.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Detailed per-source logging.",
        )
        parser.add_argument(
            "--allow-production",
            action="store_true",
            help="Required when DEBUG=False.",
        )

    def handle(self, *args, **options):
        self.verbose = options["verbose"]

        if not settings.DEBUG and not options["allow_production"]:
            raise CommandError(
                "Refusing to run in production. Use --allow-production to override."
            )

        qs = self._get_queryset(options)
        total = qs.count()
        if total == 0:
            self.stdout.write("No sources found with NULL source_type.")
            return

        sources, actual = self._apply_limit(qs, options["limit"], total)
        dry_run = options["dry_run"]

        classified, skipped, results = self._classify_batch(sources, dry_run=dry_run)
        self._print_summary(actual, classified, skipped, dry_run, results)

    def _get_queryset(self, options):
        qs = DocumentSource.objects.filter(source_type__isnull=True, is_deleted=False)
        if options["source_id"]:
            qs = qs.filter(source_id=options["source_id"])
        return qs

    def _apply_limit(self, qs, limit, total):
        if limit > 0:
            qs = qs[:limit]
            actual = len(qs)
            self.stdout.write(f"Processing up to {actual} of {total} eligible sources.")
        else:
            qs = list(qs)
            actual = len(qs)
            self.stdout.write(f"Processing all {actual} eligible sources.")
        return qs, actual

    def _classify_batch(self, sources, *, dry_run):
        classified = 0
        skipped = 0
        results: dict[str, int] = {}
        updates: dict[str, list[str]] = {}

        for source in sources:
            st = _classify_source_type(source)
            if st is None:
                skipped += 1
                if self.verbose:
                    self.stdout.write(f"[SKIP] {source.source_id}: no rule matched")
                continue

            label = st.value if hasattr(st, "value") else str(st)
            results[label] = results.get(label, 0) + 1
            classified += 1

            if dry_run:
                self._log_verbose(f"[DRY-RUN] {source.source_id}: → {label}")
            else:
                updates.setdefault(label, []).append(source.source_id)
                self._log_verbose(f"[SET] {source.source_id}: → {label}")

        if not dry_run and updates:
            for label, source_ids in updates.items():
                DocumentSource.objects.filter(source_id__in=source_ids).update(
                    source_type=label,
                    updated_at=timezone.now(),
                )

        return classified, skipped, results

    def _print_summary(self, actual, classified, skipped, dry_run, results):
        self.stdout.write("-" * 50)
        self.stdout.write(
            f"Total={actual}  Classified={classified}  "
            f"Skipped={skipped}  Dry-run={dry_run}"
        )
        if classified:
            self.stdout.write("Breakdown:")
            for label, count in sorted(results.items(), key=lambda x: -x[1]):
                self.stdout.write(f"  {label}: {count}")

    def _log_verbose(self, msg: str) -> None:
        if self.verbose:
            self.stdout.write(msg)
