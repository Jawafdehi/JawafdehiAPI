"""
Django management command to populate the timeline field on CIAA DRAFT cases
with court hearing data from NGM and dates from case metadata.

Phase 1e of CIAA FY 080/081 Case Enrichment pipeline.

Timeline entries are built from:
1. Case registration date (case_start_date or NGM registration_date_ad)
2. Court hearing dates (from NGM court_case_hearings)
3. Verdict date (case_end_date or NGM verdict_date_ad)

Each timeline entry has ``date`` (ISO format), ``title`` (Nepali), and
optional ``description``.

Idempotent: skips cases that already have non-empty timeline.

Usage::

    python manage.py enrich_ciaa_timeline --dry-run
    python manage.py enrich_ciaa_timeline --case-id case-0123
    python manage.py enrich_ciaa_timeline --limit 10 --verbose
    python manage.py enrich_ciaa_timeline --priority
    python manage.py enrich_ciaa_timeline --force
"""

import logging
from typing import Optional

from django.core.management.base import BaseCommand
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from cases.models import Case, CaseType, CaseState
from cases.services.priority_case_loader import filter_by_priority, load_priority_cases
from ngm.services import get_court_case_details

logger = logging.getLogger(__name__)

TITLE_CASE_REGISTERED = "मुद्दा दर्ता"
TITLE_HEARING = "पेशी"
TITLE_VERDICT = "फैसला"


class Command(BaseCommand):
    help = (
        "Populate timeline on CIAA DRAFT cases using NGM court hearing data "
        "and case metadata."
    )

    def __init__(self):
        super().__init__()
        self.stats = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "cases_no_court_case": 0,
            "cases_ngm_error": 0,
            "cases_already_populated": 0,
            "total_timeline_entries": 0,
        }

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview without saving to database",
        )
        parser.add_argument(
            "--case-id",
            type=str,
            help="Process a specific case by case_id",
        )
        parser.add_argument(
            "--priority",
            action="store_true",
            help="Enrich only cases in the priority case list",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="all_cases",
            help="Enrich all DRAFT CIAA cases (explicit, same as default)",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-timeline cases that already have timeline entries",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Limit number of cases to process",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose debug logging",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        case_id = options.get("case_id")
        priority = options["priority"]
        all_cases_flag = options.get("all_cases")
        force = options["force"]
        limit = options.get("limit")
        verbose = options.get("verbose")

        if verbose:
            logger.setLevel(logging.DEBUG)

        if priority and case_id:
            self.stderr.write(
                self.style.ERROR("--priority and --case-id are mutually exclusive")
            )
            return

        if dry_run:
            logger.warning("DRY-RUN MODE: No changes will be saved")

        cases = self._get_ciaa_cases(
            case_id=case_id,
            priority=priority,
            all_cases_flag=all_cases_flag,
            force=force,
            limit=limit,
        )

        total = cases.count()
        if total == 0:
            logger.info("No cases to process")
            return

        logger.info(f"Processing {total} cases")

        for idx, case in enumerate(cases, 1):
            self._process_case(case=case, idx=idx, total=total, dry_run=dry_run)

        self._print_summary(dry_run)

    def _get_ciaa_cases(
        self,
        case_id: Optional[str] = None,
        priority: bool = False,
        all_cases_flag: bool = False,
        force: bool = False,
        limit: Optional[int] = None,
    ):
        if case_id:
            cases = Case.objects.filter(
                case_id=case_id, case_type=CaseType.CORRUPTION
            )
        else:
            cases = Case.objects.filter(
                case_type=CaseType.CORRUPTION,
                state__in=[CaseState.DRAFT],
            ).order_by("created_at")

            if priority:
                priority_list = load_priority_cases()
                logger.info(
                    "Priority mode: loaded %d case numbers across all fiscal years",
                    len(priority_list),
                )
                cases = filter_by_priority(cases, priority_list)
            elif not all_cases_flag:
                logger.info(
                    "Processing all DRAFT CIAA cases (default). "
                    "Use --all to make this explicit or --priority to filter."
                )

            if not force:
                total_before = cases.count()
                cases = cases.filter(Q(timeline__isnull=True) | Q(timeline=[]))
                self.stats["cases_already_populated"] = total_before - cases.count()

        if limit is not None:
            cases = cases[:limit]

        return cases

    def _process_case(self, case: Case, idx: int, total: int, dry_run: bool):
        self.stats["cases_processed"] += 1
        self.stdout.write(f"\n[{idx}/{total}] {case.case_id} — {case.title[:80]}")

        special_ref = self._extract_special_ref(case.court_cases)
        if not special_ref:
            self.stats["cases_no_court_case"] += 1
            self.stdout.write(
                self.style.WARNING("  No special:* court case reference found — skipping")
            )
            return

        court_identifier, case_number = special_ref.split(":", 1)

        logger.debug("  Fetching NGM data for %s:%s", court_identifier, case_number)

        try:
            case_details = get_court_case_details(court_identifier, case_number)
        except (OSError, ConnectionError, RuntimeError) as exc:
            self.stats["cases_ngm_error"] += 1
            self.stdout.write(
                self.style.ERROR(f"  NGM query failed: {exc}")
            )
            return
        except Exception:
            self.stats["cases_ngm_error"] += 1
            logger.exception(
                "Unexpected error fetching NGM data for %s:%s",
                court_identifier,
                case_number,
            )
            return

        if not case_details:
            self.stats["cases_ngm_error"] += 1
            self.stdout.write(
                self.style.WARNING(
                    f"  No NGM data found for {court_identifier}:{case_number}"
                )
            )
            return

        timeline = self._build_timeline(case, case_details)

        if not timeline:
            self.stats["cases_skipped"] += 1
            self.stdout.write(
                self.style.WARNING("  No timeline entries generated — skipping")
            )
            return

        self.stats["total_timeline_entries"] += len(timeline)
        self.stdout.write(
            self.style.SUCCESS(f"  Built {len(timeline)} timeline entry/entries:")
        )
        for entry in timeline:
            desc_preview = (
                entry.get("description", "")[:60] if entry.get("description") else ""
            )
            self.stdout.write(
                f"    {entry['date']}  {entry['title']}"
                + (f" — {desc_preview}" if desc_preview else "")
            )

        if dry_run:
            self.stdout.write(
                self.style.WARNING("  [DRY RUN] Would save but --dry-run is set")
            )
        else:
            self._save_timeline(case, timeline)
            self.stats["cases_enriched"] += 1

    def _extract_special_ref(self, court_cases) -> Optional[str]:
        if not court_cases or not isinstance(court_cases, list):
            return None
        for cc in court_cases:
            if isinstance(cc, str) and cc.startswith("special:"):
                return cc
        return None

    def _build_timeline(self, case: Case, case_details: dict) -> list[dict]:
        timeline = []
        seen_dates = set()

        ngm_case = case_details.get("case", {})

        registration_date = self._resolve_registration_date(case, ngm_case)
        if registration_date and registration_date not in seen_dates:
            seen_dates.add(registration_date)
            timeline.append({
                "date": registration_date,
                "title": TITLE_CASE_REGISTERED,
                "description": (
                    f"CIAA Special Court case {ngm_case.get('case_number', '')} "
                    f"registered at {ngm_case.get('division', '') or 'Special Court'}".strip()
                ),
            })

        hearings = case_details.get("hearings", [])
        if hearings:
            if not registration_date:
                first_hearing = hearings[-1]
                first_ad = first_hearing.get("hearing_date_ad")
                if first_ad and first_ad not in seen_dates:
                    seen_dates.add(first_ad)
                    timeline.append({
                        "date": first_ad,
                        "title": TITLE_CASE_REGISTERED,
                        "description": "First known hearing date (registration date unavailable)",
                    })

            for hearing in hearings:
                hearing_date_ad = hearing.get("hearing_date_ad")
                if not hearing_date_ad:
                    continue
                if hearing_date_ad in seen_dates:
                    continue
                seen_dates.add(hearing_date_ad)

                case_status = hearing.get("case_status") or ""
                decision_type = hearing.get("decision_type") or ""
                bench = hearing.get("bench") or ""
                judge_names = hearing.get("judge_names") or ""
                remarks = hearing.get("remarks") or ""

                title = TITLE_HEARING
                description_parts = []
                if bench:
                    description_parts.append(f"Bench: {bench}")
                if judge_names:
                    description_parts.append(f"Judge(s): {judge_names}")
                if case_status:
                    description_parts.append(f"Status: {case_status}")
                if decision_type:
                    description_parts.append(f"Decision: {decision_type}")

                description = "; ".join(description_parts) if description_parts else ""

                is_verdict = (
                    "फैसला" in (decision_type or "")
                    or "फैसला" in (remarks or "")
                    or "निर्णय" in (decision_type or "")
                    or hearing.get("serial_no") == "फैसला"
                )
                if is_verdict:
                    title = TITLE_VERDICT
                    if remarks and remarks not in description:
                        description = f"{description}; Remarks: {remarks}".strip("; ")

                timeline.append({
                    "date": hearing_date_ad,
                    "title": title,
                    "description": description if description else "",
                })

        verdict_date = self._resolve_verdict_date(case, ngm_case)
        if verdict_date and verdict_date not in seen_dates:
            seen_dates.add(verdict_date)
            verdict_judge = ngm_case.get("verdict_judge") or ""
            timeline.append({
                "date": verdict_date,
                "title": TITLE_VERDICT,
                "description": (
                    f"Verdict issued by {verdict_judge}".strip()
                    if verdict_judge
                    else "Verdict issued"
                ),
            })

        timeline.sort(key=lambda e: e["date"])

        return timeline

    def _resolve_registration_date(
        self, case: Case, ngm_case: dict
    ) -> Optional[str]:
        if ngm_case.get("registration_date_ad"):
            return ngm_case["registration_date_ad"]
        if case.case_start_date:
            return case.case_start_date.isoformat()
        return None

    def _resolve_verdict_date(
        self, case: Case, ngm_case: dict
    ) -> Optional[str]:
        if ngm_case.get("verdict_date_ad"):
            return ngm_case["verdict_date_ad"]
        if case.case_end_date:
            return case.case_end_date.isoformat()
        return None

    def _save_timeline(self, case: Case, timeline: list[dict]):
        self._validate_timeline_entries(timeline)
        with transaction.atomic():
            case.timeline = timeline
            case.save(update_fields=["timeline", "updated_at"])
        logger.info("  Saved %d timeline entries to %s", len(timeline), case.case_id)

    def _validate_timeline_entries(self, timeline: list[dict]):
        for entry in timeline:
            if not isinstance(entry, dict):
                raise ValidationError(f"Timeline entry must be a dict: {entry}")
            if "date" not in entry:
                raise ValidationError(
                    f"Timeline entry missing 'date': {entry}"
                )
            if "title" not in entry:
                raise ValidationError(
                    f"Timeline entry missing 'title': {entry}"
                )

    def _print_summary(self, dry_run: bool):
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(
            self.style.SUCCESS(
                f"{'[DRY RUN] ' if dry_run else ''}Timeline enrichment complete."
            )
        )
        self.stdout.write(
            f"  Cases processed:         {self.stats['cases_processed']}"
        )
        self.stdout.write(
            f"  Cases enriched:          {self.stats['cases_enriched']}"
        )
        self.stdout.write(
            f"  Cases skipped:           {self.stats['cases_skipped']}"
        )
        self.stdout.write(
            f"  No court case reference: {self.stats['cases_no_court_case']}"
        )
        self.stdout.write(
            f"  NGM errors:              {self.stats['cases_ngm_error']}"
        )
        self.stdout.write(
            f"  Already populated:       {self.stats['cases_already_populated']}"
        )
        self.stdout.write(
            f"  Total timeline entries:  {self.stats['total_timeline_entries']}"
        )
