"""Job queue API — the one contract every consumer speaks.

    POST /api/jobs/           enqueue a unit of work
    POST /api/jobs/claim/     atomically claim the next available job (or 204)
    POST /api/jobs/<id>/stage/    progress heartbeat (extends the lease)
    POST /api/jobs/<id>/result/   finalize (done / failed / retry)
    GET  /api/jobs/           read-only dashboard (filter by ?kind=&status=)

This generalizes the review app's job endpoints; the review poller is now just a
consumer of ``kind=case_review`` here. State lives in Postgres (see jobs.queue).
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from . import queue
from .models import Job
from .permissions import CanConsumeJobs, CanObserveJobs
from .serializers import (
    JobClaimSerializer,
    JobEnqueueSerializer,
    JobResultSerializer,
    JobSerializer,
    JobStageSerializer,
)


@api_view(["GET", "POST"])
@permission_classes([CanObserveJobs])
def jobs_collection(request):
    """GET: read-only dashboard. POST: enqueue (requires consume role)."""
    if request.method == "GET":
        qs = Job.objects.all()
        kind = request.query_params.get("kind")
        st = request.query_params.get("status")
        if kind:
            qs = qs.filter(kind=kind)
        if st:
            qs = qs.filter(status=st)
        # Safe-parse ?limit=: a non-integer would raise ValueError and a negative
        # would trip the queryset-slice assertion (both → 500). Fall back to 200.
        try:
            limit = int(request.query_params.get("limit", 200))
        except (TypeError, ValueError):
            limit = 200
        if limit < 0:
            limit = 200
        qs = qs[:limit]
        return Response(JobSerializer(qs, many=True).data)

    # POST enqueue — gate on the mutating permission explicitly (the decorator
    # class only enforces the read floor for the shared collection endpoint).
    if not CanConsumeJobs().has_permission(request, None):
        return Response(
            {"detail": CanConsumeJobs.message}, status=status.HTTP_403_FORBIDDEN
        )
    s = JobEnqueueSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    d = s.validated_data
    job = queue.enqueue(
        d["kind"],
        payload=d.get("payload") or {},
        dedup_key=d.get("dedup_key"),
        priority=d.get("priority", 100),
        submitted_by=request.user if request.user.is_authenticated else None,
    )
    return Response(JobSerializer(job).data, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([CanConsumeJobs])
def claim(request):
    """Atomically claim the next available job among the requested kinds.

    Returns 204 when nothing is claimable. On success returns the full job
    (payload already enriched by the kind's server-side build_payload hook) so a
    DB-free consumer has everything it needs to run.
    """
    s = JobClaimSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    job = queue.claim_next(s.validated_data["kinds"])
    if job is None:
        return Response(status=status.HTTP_204_NO_CONTENT)
    data = JobSerializer(job).data
    data["payload"] = job.payload
    return Response(data)


def _get_job_or_404(pk):
    try:
        return Job.objects.get(pk=pk), None
    except Job.DoesNotExist:
        return None, Response(
            {"detail": "Job not found."}, status=status.HTTP_404_NOT_FOUND
        )


@api_view(["POST"])
@permission_classes([CanConsumeJobs])
def stage(request, pk):
    """Progress heartbeat: update ``stage`` and extend the lease. Best-effort."""
    job, err = _get_job_or_404(pk)
    if err:
        return err
    s = JobStageSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    queue.touch(job, stage=s.validated_data.get("stage") or "")
    return Response({"ok": True})


@api_view(["POST"])
@permission_classes([CanConsumeJobs])
def result(request, pk):
    """Finalize a claimed job. Rejects finalizing a job that is not RUNNING.

    The stale-guard (409 unless RUNNING) is what stops a retried request or a
    zombie worker — one whose lease was reaped and the job re-queued/re-claimed —
    from clobbering a done or re-queued row.
    """
    job, err = _get_job_or_404(pk)
    if err:
        return err
    s = JobResultSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    d = s.validated_data
    # finalize() owns the stale-guard: it locks the row and raises JobNotRunning
    # if the job is no longer RUNNING (reaped/re-queued by another worker), which
    # we surface as 409 — no separate, race-prone pre-check here.
    try:
        job = queue.finalize(
            job,
            status=Job.DONE if d["status"] == "done" else Job.FAILED,
            result=d.get("result"),
            error=d.get("error", ""),
            retryable=d.get("retryable", False),
            duration_seconds=d.get("duration_seconds"),
        )
    except queue.JobNotRunning as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
    return Response(JobSerializer(job).data)
