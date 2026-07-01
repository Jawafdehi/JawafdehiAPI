"""``seed_courtcases`` — DEV-ONLY: seed a few representative court cases.

The NGM court tables (``courts``/``court_cases``/``court_case_hearings``/
``court_case_entities``) ship EMPTY in local dev — only the relational *schema*
exists; the Scrapy ingestion that fills them in production has not run here. That
leaves the court-case UI (``/courtcase/*`` detail page, the unified-search
``courtcase`` result type) with nothing to render against.

This command inserts a handful of realistic, public-domain-shaped court cases
(Supreme Court + Special Court, with hearings and plaintiff/defendant parties) so
the frontend can be built and verified end-to-end. It then reindexes them into the
``ngm-courtcases`` OpenSearch index (unless ``--no-index``).

SAFETY: refuses to run unless ``DEBUG`` is true OR ``--force`` is passed — fixture
data must never land in a production ``ngm`` DB. All writes go through the DB
router to the ``ngm`` database. Idempotent: re-running updates the same natural
keys (``--purge`` first deletes only the seeded rows).

    python manage.py seed_courtcases                 # seed + index (dev)
    python manage.py seed_courtcases --purge         # clear seeded rows, re-seed
    python manage.py seed_courtcases --no-index      # seed only (skip OpenSearch)
"""

from __future__ import annotations

import datetime as _dt

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from courts import search_index
from courts.models import (
    CaseEntity,
    Court,
    CourtCase,
    CourtCaseHearing,
)

# Courts the fixtures reference (identifier is the PK + the IRI ``<court>`` segment).
_COURTS = [
    {
        "identifier": "supreme",
        "court_type": "supreme",
        "full_name_nepali": "सर्वोच्च अदालत",
        "full_name_english": "Supreme Court of Nepal",
    },
    {
        "identifier": "special",
        "court_type": "special",
        "full_name_nepali": "विशेष अदालत",
        "full_name_english": "Special Court",
    },
]

# A small, representative spread: corruption (Special Court), a writ + a criminal
# appeal (Supreme Court). Party names are illustrative public-official archetypes,
# not real individuals. ``_seed`` marks rows this command owns (for --purge).
_CASES = [
    {
        "court": "special",
        "case_number": "081-CR-0079",
        "registration_date_bs": "2081-04-12",
        "registration_date_ad": _dt.date(2024, 7, 27),
        "case_type": "CORRUPTION",
        "case_status": "ONGOING",
        "plaintiff": "नेपाल सरकार",
        "defendant": "प्रमुख जिल्ला अधिकारी (आरोपित)",
        "hearings": [
            {
                "hearing_date_bs": "2081-05-03",
                "hearing_date_ad": _dt.date(2024, 8, 18),
                "bench": "इजलास १",
                "bench_type": "single",
                "judge_names": "मा. न्या. उदाहरण",
                "case_status": "ONGOING",
                "decision_type": "PESHI",
            },
        ],
        "entities": [
            {"side": "plaintiff", "name": "नेपाल सरकार"},
            {"side": "defendant", "name": "प्रमुख जिल्ला अधिकारी (आरोपित)"},
        ],
    },
    {
        "court": "supreme",
        "case_number": "081-WO-0312",
        "registration_date_bs": "2081-03-20",
        "registration_date_ad": _dt.date(2024, 7, 4),
        "case_type": "WRIT",
        "case_status": "ONGOING",
        "plaintiff": "रिट निवेदक",
        "defendant": "नेपाल सरकार, प्रधानमन्त्री तथा मन्त्रिपरिषद्को कार्यालय",
        "hearings": [
            {
                "hearing_date_bs": "2081-06-10",
                "hearing_date_ad": _dt.date(2024, 9, 26),
                "bench": "संयुक्त इजलास",
                "bench_type": "division",
                "judge_names": "मा. न्या. उदाहरण एक, मा. न्या. उदाहरण दुई",
                "case_status": "ONGOING",
                "decision_type": "PESHI",
            },
        ],
        "entities": [
            {"side": "plaintiff", "name": "रिट निवेदक"},
            {"side": "defendant", "name": "प्रधानमन्त्री तथा मन्त्रिपरिषद्को कार्यालय"},
        ],
    },
    {
        "court": "supreme",
        "case_number": "080-CR-0146",
        "registration_date_bs": "2080-11-02",
        "registration_date_ad": _dt.date(2024, 2, 14),
        "case_type": "CRIMINAL_APPEAL",
        "case_status": "DECIDED",
        "plaintiff": "नेपाल सरकार",
        "defendant": "प्रतिवादी (पुनरावेदक)",
        "hearings": [
            {
                "hearing_date_bs": "2081-01-15",
                "hearing_date_ad": _dt.date(2024, 4, 27),
                "bench": "इजलास ३",
                "bench_type": "division",
                "judge_names": "मा. न्या. उदाहरण तीन",
                "case_status": "DECIDED",
                "decision_type": "FAISALA",
            },
        ],
        "entities": [
            {"side": "plaintiff", "name": "नेपाल सरकार"},
            {"side": "defendant", "name": "प्रतिवादी (पुनरावेदक)"},
        ],
    },
]


class Command(BaseCommand):
    help = "DEV-ONLY: seed representative NGM court cases (+ hearings/entities) and index them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Allow seeding even when DEBUG is False (use with care; never in prod).",
        )
        parser.add_argument(
            "--purge",
            action="store_true",
            help="Delete the seeded rows (by their natural keys) before re-seeding.",
        )
        parser.add_argument(
            "--no-index",
            action="store_true",
            help="Skip reindexing into OpenSearch (seed the DB only).",
        )

    def handle(self, *args, **opts):
        if not settings.DEBUG and not opts["force"]:
            raise CommandError(
                "Refusing to seed fixtures with DEBUG=False. This is dev-only data; "
                "pass --force only if you are certain this is NOT a production ngm DB."
            )

        keys = [(c["court"], c["case_number"]) for c in _CASES]

        with transaction.atomic(using="ngm"):
            # Purge inside the transaction so a mid-seed failure rolls back the
            # delete too (never leaves the rows gone with nothing re-inserted).
            if opts["purge"]:
                self._purge(keys)
            for court in _COURTS:
                Court.objects.using("ngm").update_or_create(
                    identifier=court["identifier"], defaults=court
                )
            for case in _CASES:
                self._seed_case(case)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(_CASES)} court cases across {len(_COURTS)} courts."
            )
        )

        if opts["no_index"]:
            self.stdout.write("Skipped OpenSearch indexing (--no-index).")
            return
        self._index(keys)

    def _purge(self, keys):
        for court, number in keys:
            CaseEntity.objects.using("ngm").filter(court_id=court, case_number=number).delete()
            CourtCaseHearing.objects.using("ngm").filter(court_id=court, case_number=number).delete()
            CourtCase.objects.using("ngm").filter(court_id=court, case_number=number).delete()
        self.stdout.write(f"Purged {len(keys)} seeded court cases.")

    def _seed_case(self, case: dict):
        now = timezone.now()
        CourtCase.objects.using("ngm").update_or_create(
            court_id=case["court"],
            case_number=case["case_number"],
            defaults={
                "registration_date_bs": case["registration_date_bs"],
                "registration_date_ad": case["registration_date_ad"],
                "case_type": case["case_type"],
                "case_status": case["case_status"],
                "plaintiff": case["plaintiff"],
                "defendant": case["defendant"],
                "extra_data": {"_seed": True},
            },
        )
        # Hearings + entities have autoincrement PKs and no composite uniqueness,
        # so clear-then-insert keeps re-runs idempotent.
        CourtCaseHearing.objects.using("ngm").filter(
            court_id=case["court"], case_number=case["case_number"]
        ).delete()
        for h in case["hearings"]:
            CourtCaseHearing.objects.using("ngm").create(
                court_id=case["court"],
                case_number=case["case_number"],
                scraped_at=now,
                extra_data={"_seed": True},
                **h,
            )
        CaseEntity.objects.using("ngm").filter(
            court_id=case["court"], case_number=case["case_number"]
        ).delete()
        for e in case["entities"]:
            CaseEntity.objects.using("ngm").create(
                court_id=case["court"],
                case_number=case["case_number"],
                **e,
            )

    def _index(self, keys):
        # Use the LOUD path (upsert_doc directly), not search_index.index() — the
        # latter is @best_effort and swallows OpenSearch errors, which would let
        # this command report success while ngm-courtcases silently diverges from
        # PG. A cluster error here should fail the command so the operator knows
        # to reindex.
        from jawafdehi_shared.search.opensearch import COURTCASE_INDEX, make_client
        from jawafdehi_shared.search.indexing import upsert_doc

        client = make_client()
        indexed = 0
        for court, number in keys:
            obj = (
                CourtCase.objects.using("ngm")
                .select_related("court")
                .get(court_id=court, case_number=number)
            )
            upsert_doc(client, COURTCASE_INDEX, search_index.build_doc(obj))
            indexed += 1
        self.stdout.write(
            self.style.SUCCESS(f"Indexed {indexed} court cases into ngm-courtcases.")
        )
