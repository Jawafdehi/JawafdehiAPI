# Central Job Queue — Design Doc

**Status:** BUILT (phases 1–3 shipped) · **Date:** 2026-07-01 · **Branch:** `feat/control-plane-design`

> **As-built (2026-07-01):** the `jobs` app, queue engine, API, reaper command, and
> the `case_review` port are implemented and tested (37 new unit tests; full
> in-process suite green at 1014 passed). The review poller is now a generic jobs
> consumer (`--kinds case_review`). Remaining from §5: migration steps 3–5 (retire
> the `review_batch_monitor` babysitter operationally; add `material_convert` as the
> second kind; fold in reindex/enrich). Open decisions §6 were resolved with the
> baked-in recommendations: HTTP-poller sole contract, lazy-sweep reaper + safety
> cron (`manage.py reap_jobs`), scheduling out of scope, `CaseReview` finalized via
> the `on_result`/`on_failure` hooks (not derived — kept its own row, synced by hook).
**Companion:** the async material-conversion flow (control-plane §3) is the first
*new* consumer; the existing casework **review poller** is the pattern this
generalizes and the first consumer to port.

**Framing:** the platform already has a working, correct Postgres-backed queue —
it just lives inside the `review` app and only knows one job type. This doc
promotes it to a **central `jobs` app**: one atomic-claim queue, one lease/reaper,
one retry policy, one dashboard, with per-**kind** handlers. The review poller
stops *being* the queue and **becomes a queue consumer**.

---

## 0. Decision (locked 2026-07-01)

**Postgres-backed job queue. No broker (no Celery, no RabbitMQ, no Redis).**

Why not a broker — recorded so it is not re-litigated:
- **RabbitMQ vs Celery is a category error.** RabbitMQ is a transport; Celery is a
  task framework that needs a broker under it. "Raw RabbitMQ" still forces you to
  build retries/leases/results/dashboard by hand — *more* work than Celery — and
  you need a DB table for queue observability anyway.
- **The workload is a handful of long-running batch jobs** (a 300-page CIAA OCR run
  is minutes; review is minutes). Celery's real strengths — thousands/sec
  throughput, not-writing-retry-logic — are a weak match. The retry/lease logic we
  "save" is small and already half-built (`select_for_update(skip_locked=True)`).
- **A broker breaks the consumer model we want to keep.** The review poller is
  DB-free, OIDC-service-account, HTTP (`claim → stage → result`). A Celery worker
  connects to the broker directly (broker creds, not OIDC) and stores state in a
  result backend (not our Postgres tables) — killing the `GET /api/jobs` dashboard,
  the `--apply`/read-only safety default, and transactional enqueue.
- **Postgres keeps enqueue transactional with the source of truth** — enqueuing a
  job is a row insert in the same DB transaction as the domain write. A broker needs
  the outbox pattern to get this right.
- **Zero new infra**, consistent with the platform's standing discipline (no Iceberg
  for 10³–10⁵ rows; OpenSearch is derived/lossy, never the queue).

**Trigger to revisit:** sustained thousands-of-enqueues/sec, or broad fan-out to many
heterogeneous worker fleets. Neither is on the horizon. If it comes, the `jobs`
`kind`+handler abstraction is exactly what ports onto Celery — building it now is not
wasted even in the broker future.

---

## 1. What already exists (the pattern we generalize)

The `review` app is a correct single-purpose queue. The parts worth keeping:

- **`CaseReview`** (`review/models.py`) is the queue table: `status`
  (pending→running→done→failed), `stage`, `started_at`, `completed_at`,
  `duration_seconds`, `error`, `result` JSON.
- **`POST /jobs/claim/`** (`review/views.py:86`) is an **atomic dequeue** done right:
  `select_for_update(skip_locked=True).filter(status=PENDING).order_by("id").first()`,
  then flips to `RUNNING` + stamps `started_at` inside the transaction. This is the
  hard primitive — it is already here and correct.
- **`submit_job_result`** (`review/views.py:172`) has a **stale-completion guard**:
  it rejects finalizing a row that is not `RUNNING` (409), so a retried/zombie
  submission can't clobber a done or re-queued row.
- **The poller** (`review/management/commands/review_poller.py`) is **DB-free,
  OIDC-service-account, HTTP-only** (`claim → stage → result`), horizontally
  scalable (claim is atomic), **read-only by default** with `--apply` to mutate, and
  `--once` to drain-then-exit.

What it lacks (and can't easily grow without a design): **leases** (a crashed worker
strands a `running` row forever — today patched by the external `review_batch_monitor`
babysitter), **retries/backoff**, **dedup**, **priority**, and a **cross-kind
dashboard**. Those are exactly what the central manager adds *once*.

---

## 2. The `jobs` app

New Django app on the **`default` DB** (queue is platform-wide, not NGM/NES data).

### 2.1 Model

```
jobs/models.py

class Job(models.Model):
    QUEUED, RUNNING, DONE, FAILED, DEAD = "queued","running","done","failed","dead"

    kind             CharField   # "case_review" | "material_convert" | "reindex" | ...
    status           CharField   # queued → running → done | failed | dead
    priority         IntegerField default=100      # lower = sooner
    payload          JSONField                      # handler input
    result           JSONField    null=True         # handler output
    stage            CharField    blank             # progress ping
    dedup_key        CharField    null=True, unique # natural key of the work unit
    attempts         IntegerField default=0
    max_attempts     IntegerField default=3
    lease_expires_at DateTimeField null=True        # set on claim, cleared on finalize
    available_at     DateTimeField default=now      # claim only where <= now (delay/backoff)
    error            TextField    blank
    submitted_by     FK(User)     null=True
    created_at / started_at / completed_at / updated_at

    class Meta:
        indexes = [Index(fields=["status","kind","priority","available_at"])]  # covers claim
```

**Separation of concerns:** the `Job` holds *lifecycle + scheduling*. Domain records
(`CaseReview`, `Material`) keep their own tables and link to a `Job`. `CaseReview`
stops being a queue and becomes a review record that *has* a job; its display
`status` is **derived** from `job.status` (single source of truth) — see §6 open Q.

### 2.2 Claim — generalized from `review/views.py:86`

```python
def claim_next(kinds: list[str]) -> Job | None:
    reap_expired()  # lazy sweep, see §3
    with transaction.atomic():
        job = (Job.objects
            .select_for_update(skip_locked=True)
            .filter(status=Job.QUEUED, kind__in=kinds, available_at__lte=now())
            .order_by("priority", "available_at", "id")   # priority, then FIFO
            .first())
        if job is None:
            return None
        job.status = Job.RUNNING
        job.attempts += 1
        job.started_at = now()
        job.lease_expires_at = now() + lease_for(job.kind)
        job.stage = "claimed"
        job.save(update_fields=[...])
        return job
```

`skip_locked` is what makes it multi-consumer safe — unchanged from today. The only
additions to the existing query are the `priority`/`available_at` ordering and the
lease stamp.

---

## 3. The three properties the poller can't have

**Leases + reaper (orphaned-worker fix, made structural).** A crashed worker leaves
a `running` job with a lapsed `lease_expires_at`. `reap_expired()` finds
`status=running AND lease_expires_at < now()` and either re-queues it
(`attempts < max_attempts` → `queued`, `available_at = now()+backoff`) or marks it
`dead`. This **replaces the external `review_batch_monitor` babysitter** with a queue
property. Long-legitimate jobs (300-page OCR) extend their lease via the existing
`stage` heartbeat.
- *Reaper placement (rec):* **lazy sweep inside `claim_next`** (a claim also reclaims
  one expired job → zero extra process) **+ a slow safety-net cron** for low-claim
  periods. (Open Q §6.)

**Retries with backoff.** On failure the handler reports `retryable=true`; the
manager returns the job to `queued` with `available_at = now() + backoff(attempts)`.
Exhaust `max_attempts` → `dead` (dead-letter state). This is what makes unattended
batch OCR safe — a page-250 failure resumes instead of vanishing.

**Dedup.** `dedup_key` unique constraint: enqueuing
`material_convert:ciaa/annual-report-2081` twice is a no-op (catch IntegrityError,
return the existing job). Stops double-OCR.

---

## 4. API contract (keep the poller's protocol, generalize the noun)

The poller already speaks `claim/stage/result`. The manager keeps that shape,
kind-parameterized and un-scoped from `/reviews`:

```
POST /api/jobs/claim        {kinds: ["material_convert"]}      → job payload | 204
POST /api/jobs/{id}/stage   {stage: "ocr:page 40/300"}         → extends lease
POST /api/jobs/{id}/result  {status, result|error, retryable?} → finalize (stale-guarded)
POST /api/jobs              {kind, payload, dedup_key?, priority?} → enqueue
GET  /api/jobs?kind=&status=                                    → observability/dashboard
```

- Auth: same OIDC service-account model as the review poller today (a Zitadel service
  account with the relevant role; `HasNgmRole` / caseworker per kind).
- `submit_job_result`'s **stale-guard carries over and matters more** now — with
  retries/reaper able to re-queue, a zombie worker submitting late must get 409, not
  clobber.
- `GET /api/jobs` is the cross-kind dashboard the single-purpose poller never had. (We
  *observe* backlog here; the queue itself lives in Postgres — OpenSearch is never the
  queue.)

**Execution model (rec, open Q §6):** HTTP poller is *the one contract* — keeps heavy
deps (PyMuPDF, likhit, Bedrock SDK) and CPU load off API pods, matches today. Cheap
in-process workers, if wanted, are just a second client of the same endpoints.

---

## 5. Migration path (incremental, parity-first)

1. **Build `jobs`** app + claim/reaper/retry/API. Ship dormant (no consumers).
2. **Port `case_review`** onto it. `submit_review` (`review/views.py:71`) now creates
   the `CaseReview` **and** enqueues `Job(kind="case_review", payload={slug},
   dedup_key=slug)`. Claim resolves the case dict server-side (existing
   `case_provider.get_case`, `views.py:120`) into the payload so the poller stays
   DB-free. The poller runs `--kinds case_review`; `runner.process_case` becomes the
   registered handler; its HTTP calls point at `/api/jobs/*`. **Prove parity** against
   the live review path (same cases in → same results out) — this is the risk gate,
   review is production.
3. **Retire** `/reviews/jobs/*` and the `review_batch_monitor` babysitter (reaper
   replaces it).
4. **Add `material_convert`** as a native second kind: new handler (OCR→markdown→index,
   the async-conversion flow) + `review_poller --kinds material_convert` (or a combined
   consumer). Now the abstraction pays for itself.
5. **Reindex/enrich** management commands (`reindex_*`, `bulk_ingest`, `enrich_ciaa_*`)
   become kinds opportunistically.

The consumer barely changes: same binary, same `claim/stage/result` protocol, same
OIDC + `--apply` safety, new `--kinds`. That is the whole point of the promotion.

---

## 6. Open decisions (need a ruling; recs baked in)

1. **Execution model** — HTTP-poller as the sole contract (rec), or also allow
   in-process workers for cheap jobs? Shapes the whole worker story.
2. **Reaper placement** — lazy-sweep inside `claim` (rec) + slow safety cron, vs a
   standalone timer command only.
3. **Scheduling** — does the queue own recurring/cron jobs (reindex-nightly), or only
   on-demand enqueues? *Rec: out of scope v1 — pure work queue; a thin recurring
   enqueuer sits on top later.*
4. **`CaseReview.status`** — derived from `job.status` (rec, one source of truth) vs
   duplicated+synced. Derived touches the serializers the frontend reads.
5. **Per-kind lease durations & `max_attempts`** — e.g. `case_review` 10 min / 3
   tries; `material_convert` 30 min (300-page OCR) / 2 tries. Set per kind in the
   handler registry.

---

## 7. What is NOT in scope

- No broker, no external queue service (see §0).
- No cross-DB queue — `jobs` lives on `default`; NGM/NES data DBs are untouched.
- No new auth model — reuse the OIDC service-account pattern the poller already uses.
- Recurring/scheduled jobs (v1 is on-demand only; §6.3).
