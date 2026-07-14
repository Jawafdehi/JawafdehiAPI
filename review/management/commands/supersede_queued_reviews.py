"""One-shot sweep: dead-letter duplicate QUEUED case_review jobs per case.

Before ``_enqueue_review_job`` learned to supersede-on-enqueue (and
``regrade_all`` learned to regrade only the latest review per case), repeated
submits/regrades piled up many queued jobs for the same case — each a full LLM
run over identical content, since the case dict is resolved live at claim time.
This command collapses that backlog: for every case it keeps ONLY the newest
queued job and supersedes the rest via ``review.supersede`` (job -> DEAD, its
CaseReview -> failed/"superseded"). RUNNING jobs are never touched.

Read-only by default (reports what would be superseded); --apply mutates.

  manage.py supersede_queued_reviews           # dry-run: report duplicates
  manage.py supersede_queued_reviews --apply   # actually supersede
"""

from collections import defaultdict

from django.core.management.base import BaseCommand

from jobs.models import Job
from review.supersede import supersede_older_queued_jobs


class Command(BaseCommand):
    help = "Keep only the newest queued case_review job per case; dead-letter the rest."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually supersede (job -> dead, review -> failed). Default: dry-run report.",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]

        by_case = defaultdict(list)
        for job_id, payload in (
            Job.objects.filter(kind="case_review", status=Job.QUEUED)
            .order_by("id")
            .values_list("id", "payload")
        ):
            case_id = (payload or {}).get("case_id")
            if case_id:
                by_case[case_id].append(job_id)

        dupes = {case_id: ids for case_id, ids in by_case.items() if len(ids) > 1}
        total_stale = sum(len(ids) - 1 for ids in dupes.values())
        self.stdout.write(
            f"queued case_review jobs: {sum(len(v) for v in by_case.values())} "
            f"over {len(by_case)} case(s); {total_stale} stale duplicate(s) "
            f"across {len(dupes)} case(s)"
        )

        superseded = 0
        for case_id, ids in sorted(dupes.items()):
            keep = ids[-1]  # newest enqueue wins
            if apply:
                n = supersede_older_queued_jobs(case_id, keep_job_id=keep)
                superseded += n
                self.stdout.write(f"  case {case_id}: kept job {keep}, superseded {n}")
            else:
                self.stdout.write(
                    f"  case {case_id}: would keep job {keep}, supersede {len(ids) - 1} "
                    f"({', '.join(str(i) for i in ids[:-1])})"
                )

        if apply:
            self.stdout.write(self.style.SUCCESS(f"superseded {superseded} job(s)."))
        else:
            self.stdout.write(
                "dry-run only — re-run with --apply to supersede the duplicates."
            )
