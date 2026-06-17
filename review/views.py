"""Casework Review System API views (ported into jawafdehi-api).

Auth model:
  - Clients send an OIDC access token as
    `Authorization: Bearer <access>`; OIDCJWTAuthentication validates it and
    syncs the caller's roles into Django Groups.
  - Read endpoints (review list/detail, rules, config GET, ``me``) require a
    role with read access (CanReadReview: Contributor+ / ReviewAssistant /
    the org-wide ReadOnly role). Mutation endpoints require at least the
    Contributor role (HasContributorRole), which excludes ReadOnly.

There is no login_view here; the SPA authenticates against the OIDC provider. We
expose a small `me` view so the SPA can show who is signed in and gate the UI
by role.
"""

import structlog
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from config.db_router import force_primary_reads

from . import case_provider, code_rules
from .models import CaseReview, ReviewConfig
from .pagination import ReviewResultsPagination
from .permissions import (
    CanManageDocumentSources,
    CanReadReview,
    HasContributorRole,
    IsAdminOrModerator,
)
from .serializers import (
    CaseReviewDetailSerializer,
    CaseReviewListSerializer,
    JobResultSerializer,
    ReviewConfigSerializer,
    SourceMarkdownSerializer,
    SubmitSerializer,
)

_audit_log = structlog.get_logger("jawafdehi.audit")


@api_view(["POST"])
@permission_classes([HasContributorRole])
def submit_review(request):
    """Submit a case for review by slug or court case number.

    The case is verified up front (so a bad slug / unknown court case number
    fails fast with the case title pulled for display), and only one review may
    be active per case: a new submission is rejected while an earlier review for
    the same case is still pending or running. Creates the review as `pending`;
    the out-of-process poller claims it via the job API and runs it. Nothing
    executes in-process here.
    """
    s = SubmitSerializer(data=request.data)
    s.is_valid(raise_exception=True)

    try:
        slug, title = case_provider.resolve_target(
            slug=s.validated_data["slug"] or None,
            court_case_number=s.validated_data["court_case_number"] or None,
        )
    except case_provider.CaseNotFound as e:
        return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)

    # The pre-check gives a friendly message in the common case; the DB partial
    # unique constraint (uniq_active_review_per_case) is the source of truth and
    # closes the check-then-create race between concurrent submits.
    try:
        with transaction.atomic():
            review = CaseReview.objects.create(
                slug=slug, case_title=title, submitted_by=request.user
            )
    except IntegrityError:
        active = CaseReview.objects.filter(
            slug=slug,
            status__in=[CaseReview.STATUS_PENDING, CaseReview.STATUS_RUNNING],
        ).first()
        return Response(
            {
                "detail": (
                    f"A review for '{slug}' is already "
                    f"{active.status if active else 'active'}; "
                    f"wait for it to finish before submitting another."
                ),
                "review_id": active.id if active else None,
            },
            status=status.HTTP_409_CONFLICT,
        )
    return Response(
        CaseReviewDetailSerializer(review).data, status=status.HTTP_201_CREATED
    )


# ---------------- Job API (for the DB-free poller) ----------------
# The poller no longer reads/writes the DB directly. It claims a pending review
# here (getting the case dict + config it needs), processes it locally
# (including likhit conversion), and posts the result back. The API is the only
# component that touches the CaseReview / ReviewConfig tables.


@api_view(["POST"])
@permission_classes([HasContributorRole])
def claim_job(request):
    """Atomically claim the oldest pending review and return its work payload.

    Returns 204 when the queue is empty. On success returns everything the
    poller needs to run the review without any DB access: the review id, slug,
    the resolved case dict, and the active review config.
    """
    # force_primary_reads keeps the SELECT ... FOR UPDATE on the primary
    # connection enrolled in this transaction; otherwise the router would send
    # the read to the replica (a connection outside the atomic block).
    with force_primary_reads(), transaction.atomic():
        review = (
            CaseReview.objects.select_for_update(skip_locked=True)
            .filter(status=CaseReview.STATUS_PENDING)
            .order_by("id")
            .first()
        )
        if review is None:
            return Response(status=status.HTTP_204_NO_CONTENT)
        review.status = CaseReview.STATUS_RUNNING
        review.stage = "claimed"
        review.started_at = timezone.now()
        review.duration_seconds = None
        review.error = ""
        review.save(
            update_fields=[
                "status",
                "stage",
                "started_at",
                "duration_seconds",
                "error",
                "updated_at",
            ]
        )

    # Resolve the case dict server-side so the poller needs no case DB access.
    try:
        case = case_provider.get_case(review.slug)
        case.setdefault("slug", review.slug)
    except Exception as e:  # noqa: BLE001 - case lookup failure -> fail the job
        review.status = CaseReview.STATUS_FAILED
        review.stage = "failed"
        review.error = f"Could not load case '{review.slug}': {e}"
        review.completed_at = timezone.now()
        review.save(
            update_fields=[
                "status",
                "stage",
                "error",
                "completed_at",
                "updated_at",
            ]
        )
        return Response({"detail": review.error}, status=status.HTTP_409_CONFLICT)

    cfg = ReviewConfig.get_active()
    return Response(
        {
            "review_id": review.id,
            "slug": review.slug,
            "case": case,
            "config": {
                "pass_threshold": cfg.pass_threshold,
                "revise_threshold": cfg.revise_threshold,
                "llm_samples": cfg.llm_samples,
            },
        }
    )


@api_view(["POST"])
@permission_classes([HasContributorRole])
def job_stage(request, pk):
    """Optional progress ping from the poller (best-effort; updates `stage`)."""
    try:
        review = CaseReview.objects.get(pk=pk)
    except CaseReview.DoesNotExist:
        return Response(
            {"detail": "Review not found."}, status=status.HTTP_404_NOT_FOUND
        )
    stage = (request.data.get("stage") or "").strip()
    if stage:
        review.stage = stage[:64]
        review.save(update_fields=["stage", "updated_at"])
    return Response({"ok": True})


@api_view(["POST"])
@permission_classes([HasContributorRole])
def submit_job_result(request, pk):
    """Receive the poller's computed result and finalize the review row."""
    try:
        review = CaseReview.objects.get(pk=pk)
    except CaseReview.DoesNotExist:
        return Response(
            {"detail": "Review not found."}, status=status.HTTP_404_NOT_FOUND
        )

    # Only a RUNNING review may be finalized. This rejects a stale/duplicate
    # result (e.g. a retried request, or a poller submitting after the review
    # was re-queued by regrade-all) clobbering a done or pending row.
    if review.status != CaseReview.STATUS_RUNNING:
        return Response(
            {"detail": f"Review {pk} is not running (status={review.status})."},
            status=status.HTTP_409_CONFLICT,
        )

    s = JobResultSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    d = s.validated_data

    if d["status"] == "failed":
        review.status = CaseReview.STATUS_FAILED
        review.stage = "failed"
        review.error = d["error"]
    else:
        review.status = CaseReview.STATUS_DONE
        review.stage = "complete"
        review.error = ""
        review.case_title = d["case_title"]
        review.case_state = d["case_state"]
        review.case_type = d["case_type"]
        review.source_count = d["source_count"]
        review.sources_converted = d["sources_converted"]
        review.result = d["result"]
    review.completed_at = timezone.now()
    if d.get("duration_seconds") is not None:
        review.duration_seconds = d["duration_seconds"]
    review.save()
    return Response(CaseReviewDetailSerializer(review).data)


@api_view(["POST"])
@permission_classes([CanManageDocumentSources])
def attach_source_markdown(request, source_id):
    """Attach likhit-converted Markdown to a DocumentSource as a MARKDOWN url.

    The poller posts the markdown text it produced locally; the server stores it
    as an upload on the source and records a MARKDOWN-role link. Idempotent: a
    source that already has a MARKDOWN url is left as-is unless overwrite=true.
    Requires the change_documentsource permission (ReviewAssistant+).
    """
    from cases.models import DocumentSource
    from cases.services.source_markdown import attach_markdown

    s = SourceMarkdownSerializer(data=request.data)
    s.is_valid(raise_exception=True)

    try:
        source = DocumentSource.objects.get(source_id=source_id)
    except DocumentSource.DoesNotExist:
        return Response(
            {"detail": f"DocumentSource '{source_id}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    out = attach_markdown(
        source,
        s.validated_data["markdown"],
        overwrite=s.validated_data.get("overwrite", False),
    )
    return Response(out, status=status.HTTP_200_OK)


class ReviewListView(generics.ListAPIView):
    serializer_class = CaseReviewListSerializer
    permission_classes = [CanReadReview]
    pagination_class = ReviewResultsPagination

    def get_queryset(self):
        # Deterministic ordering (created_at can tie) so paging through the list
        # for lazy loading doesn't drop or duplicate rows across page boundaries.
        return CaseReview.objects.order_by("-created_at", "-id")


class ReviewDetailView(generics.RetrieveAPIView):
    serializer_class = CaseReviewDetailSerializer
    permission_classes = [CanReadReview]
    queryset = CaseReview.objects.all()


# ---------------- Rules (code-enforced, read-only) ----------------
# Rules now live entirely in code (review/rule_defaults.py via code_rules.py).
# They are surfaced read-only for display; there is no create/edit/delete/reset.


@api_view(["GET"])
@permission_classes([CanReadReview])
def rules_list(request):
    """List all (code-defined) rules, in display order."""
    return Response([code_rules.as_dict(r) for r in code_rules.get_rules()])


@api_view(["GET"])
@permission_classes([CanReadReview])
def rule_detail(request, pk):
    """Retrieve a single (code-defined) rule by its stable id."""
    rule = code_rules.get_rule(pk)
    if rule is None:
        return Response({"detail": "Rule not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(code_rules.as_dict(rule))


@api_view(["GET", "PUT"])
@permission_classes([CanReadReview])
def config_view(request):
    """Get or edit global review config (thresholds + LLM sampling).

    Any contributor may read the config, but only Admin / Moderator may change
    it — the thresholds are global and affect every review's disposition.
    """
    cfg = ReviewConfig.get_active()
    if request.method == "GET":
        return Response(ReviewConfigSerializer(cfg).data)
    if not IsAdminOrModerator().has_permission(request, None):
        return Response(
            {"detail": IsAdminOrModerator.message},
            status=status.HTTP_403_FORBIDDEN,
        )
    s = ReviewConfigSerializer(cfg, data=request.data, partial=True)
    s.is_valid(raise_exception=True)
    s.save()
    return Response(ReviewConfigSerializer(cfg).data)


@api_view(["POST"])
@permission_classes([HasContributorRole])
def regrade_all(request):
    """Re-queue the latest review of each case for regrading against current rules.

    Only the most recent review per slug is re-queued, and only for cases that
    do not already have an active (pending/running) review. This honors the
    one-active-review-per-case constraint (uniq_active_review_per_case): the old
    "reset every row to pending" behavior would violate it for any case with
    more than one historical review. The out-of-process poller then claims the
    re-queued reviews (each fans its LLM rules out in parallel).
    """
    active_slugs = set(
        CaseReview.objects.filter(
            status__in=[CaseReview.STATUS_PENDING, CaseReview.STATUS_RUNNING]
        ).values_list("slug", flat=True)
    )
    ids = list(
        CaseReview.objects.exclude(slug__in=active_slugs)
        .values("slug")
        .annotate(latest_id=Max("id"))
        .values_list("latest_id", flat=True)
    )
    CaseReview.objects.filter(id__in=ids).update(
        status=CaseReview.STATUS_PENDING,
        stage="queued_for_regrade",
        error="",
        updated_at=timezone.now(),
    )
    # Bulk .update() bypasses auditlog signals. Per-row LogEntry would be
    # excessive here (potentially thousands of reviews), so record one audit
    # line attributing the actor and the scope of the regrade.
    actor = getattr(request, "user", None)
    _audit_log.info(
        "casework.regrade_all",
        actor=getattr(actor, "username", None),
        actor_id=getattr(actor, "id", None),
        review_count=len(ids),
        review_ids=ids,
    )
    return Response({"regrading": len(ids), "review_ids": ids})
