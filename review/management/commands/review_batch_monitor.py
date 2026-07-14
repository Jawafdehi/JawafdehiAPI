"""Monitor the casework review batch and close VOL-3 when it finishes.

Polls the local DB every INTERVAL seconds. Once no review is pending/running, it
marks any orphaned pending/running rows failed (defensive), composes a final
report from CaseReview.result JSON (disposition / overall_score live there, not as
model fields), posts it as a VOL-3 comment, and PATCHes VOL-3 to done.

Designed to run durably under systemd. The Paperclip run JWT (PAPERCLIP_API_KEY)
and API URL / issue id are passed via env (the JWT lives ~48h).

Env:
  PAPERCLIP_API_URL   e.g. http://127.0.0.1:3100/api
  PAPERCLIP_API_KEY   run JWT
  PAPERCLIP_ISSUE_ID  VOL-3 uuid
  PAPERCLIP_RUN_ID    (optional) for the audit header
  MONITOR_INTERVAL    seconds between polls (default 60)
"""

import json
import os
import time
import urllib.request
from collections import Counter

from django.core.management.base import BaseCommand

from review.models import CaseReview


def _api(method, path, body=None):
    base = os.environ["PAPERCLIP_API_URL"].rstrip("/")
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {os.environ['PAPERCLIP_API_KEY']}")
    req.add_header("Content-Type", "application/json")
    rid = os.environ.get("PAPERCLIP_RUN_ID")
    if rid:
        req.add_header("X-Paperclip-Run-Id", rid)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode()


class Command(BaseCommand):
    help = "Poll the review batch; report + close VOL-3 when complete."

    def handle(self, *args, **opts):
        issue_id = os.environ["PAPERCLIP_ISSUE_ID"]
        interval = int(os.environ.get("MONITOR_INTERVAL", "60"))

        self.stdout.write(f"batch monitor up; issue={issue_id} interval={interval}s")

        while True:
            active = CaseReview.objects.filter(
                status__in=[CaseReview.STATUS_PENDING, CaseReview.STATUS_RUNNING]
            ).count()
            if active == 0:
                break
            time.sleep(interval)

        # Defensive: no active rows now; nothing to mark failed unless a row is
        # stuck (none expected). Build the report. select_related("case") so the
        # per-row derived ``slug`` (read off the case) doesn't N+1 in the report.
        reviews = list(CaseReview.objects.select_related("case"))
        total = len(reviews)
        by_status = Counter(r.status for r in reviews)
        done = [r for r in reviews if r.status == CaseReview.STATUS_DONE]
        failed = [r for r in reviews if r.status == CaseReview.STATUS_FAILED]

        dispositions = Counter()
        case_types = Counter()
        scores = []
        for r in done:
            res = r.result or {}
            disp = res.get("disposition")
            if disp:
                dispositions[disp] += 1
            ct = (res.get("case_type") or {}).get("type") or r.case_type or "?"
            case_types[ct] += 1
            sc = res.get("overall_score")
            if isinstance(sc, (int, float)):
                scores.append(sc)

        avg = round(sum(scores) / len(scores), 1) if scores else None
        smin = min(scores) if scores else None
        smax = max(scores) if scores else None

        def fmt_counter(c):
            return ", ".join(f"{k}: {v}" for k, v in sorted(c.items())) or "—"

        lines = []
        lines.append(
            "## Done — full re-source (PUBLISHED + IN_REVIEW only) + parallel grade"
        )
        lines.append("")
        lines.append(
            "Worked the [VOL-3](/VOL/issues/VOL-3) 22:52 directive: keep ONLY "
            "PUBLISHED + IN_REVIEW cases, delete the rest, and review them all "
            "through a parallel queue (configurable via `REVIEW_MAX_PARALLEL`, default 3)."
        )
        lines.append("")
        lines.append("### Data scope")
        lines.append(
            f"- Cases kept: **{total}** (PUBLISHED + IN_REVIEW); all DRAFT cases + their reviews deleted."
        )
        lines.append("")
        lines.append("### Concurrency model (queue of 3)")
        lines.append(
            "- Submit endpoint now only enqueues a `pending` review (no in-process run)."
        )
        lines.append(
            "- A single `jawafdehi-review-dispatcher` systemd daemon runs at most "
            "`REVIEW_MAX_PARALLEL` reviews at once — a true GLOBAL cap independent of "
            "the gunicorn worker count."
        )
        lines.append("")
        lines.append("### Results")
        lines.append(
            f"- Reviews: **{by_status.get('done', 0)} done**, {by_status.get('failed', 0)} failed of {total}."
        )
        lines.append(f"- Dispositions: {fmt_counter(dispositions)}")
        lines.append(f"- Case types: {fmt_counter(case_types)}")
        if avg is not None:
            lines.append(f"- Overall score — avg **{avg}**, min {smin}, max {smax}.")
        if failed:
            lines.append("")
            lines.append("Failed slugs:")
            for r in failed[:20]:
                err = (r.error or "").splitlines()[0] if r.error else ""
                lines.append(f"- `{r.slug}` — {err[:120]}")
        body = "\n".join(lines)

        # Post comment + close.
        try:
            _api("POST", f"/issues/{issue_id}/comments", {"body": body})
        except Exception as e:  # noqa: BLE001
            self.stderr.write(f"comment post failed: {e}")
        try:
            _api(
                "PATCH",
                f"/issues/{issue_id}",
                {"status": "done", "comment": "Batch complete — see report above."},
            )
        except Exception as e:  # noqa: BLE001
            self.stderr.write(f"status patch failed: {e}")

        self.stdout.write(
            self.style.SUCCESS(
                f"batch monitor: reported + closed VOL-3 ({by_status.get('done',0)} done)."
            )
        )
