"""Casework Review System API views (ported into jawafdehi-api).

Auth model change vs. the standalone casework system:
  - The standalone app issued its own DRF auth token via /auth/login/.
  - As of the phase5 OIDC-only migration, clients authenticate with a Zitadel
    OIDC access token sent as `Authorization: Bearer <access>` (validated by
    jawafdehi_shared.auth.oidc.OIDCAuthentication). The old SimpleJWT
    /api/caseworker/auth/token/ mint endpoint has been removed.
  - Read endpoints (review list/detail, rules, config GET, ``me``) require a
    role with read access (CanReadReview: Caseworker+ / ReviewAssistant /
    the org-wide ReadOnly role; Public is excluded). Mutation endpoints require
    at least the Caseworker role (HasContributorRole), which excludes ReadOnly and Public.

So there is no login_view here anymore; the SPA logs in against the shared JWT
endpoint. We expose a small `me` view so the SPA can show who is signed in and
gate the UI by role.
"""

from django.db.models import Max
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.response import Response

from . import code_rules
from .models import CaseReview, ReviewConfig
from .permissions import (
    CanReadReview,
    HasContributorRole,
    IsAdminOrModerator,
)
from .serializers import (
    CaseReviewDetailSerializer,
    CaseReviewListSerializer,
    ReviewConfigSerializer,
    SubmitSerializer,
)


def _user_roles_payload(user):
    """Shared shape for me/dev-login: username + real group roles + is_admin.

    v3 authz model: admin == Django superuser (there is no ``Admin`` group), so
    we no longer inject a synthetic ``"Admin"`` into ``roles``. ``roles`` carries
    only the user's real group names (e.g. ``["Caseworker"]``); admin-ness is
    conveyed by the ``is_admin`` bool. Clients MUST read ``is_admin`` for
    admin-gating — a superuser has an empty ``roles`` list.
    """
    return {
        "username": user.username,
        "roles": list(user.groups.values_list("name", flat=True)),
        "is_admin": user.is_superuser,
    }


# me_view is defined below, after DEV_OR_OIDC_AUTH — it must accept the dev
# session as well as an OIDC bearer, so its authenticators are pinned explicitly.


# ---------------------------------------------------------------------------
# DEV-ONLY username/password login for the SPA (mirrors DEV_AUTH on the backend).
#
# Production is OIDC/Zitadel only. When settings.DEV_AUTH is enabled (DEBUG or
# TESTING only — never in prod), we expose a session login the React /admin can
# POST to with the SAME credentials as the Django admin, so a developer can work
# without standing up Zitadel. These routes are ONLY mounted when DEV_AUTH is on
# (see review/urls.py); with the flag off they don't exist → SSO-only.
# ---------------------------------------------------------------------------
from django.conf import settings  # noqa: E402
from django.contrib.auth import authenticate, login, logout  # noqa: E402
from django.middleware.csrf import get_token  # noqa: E402
from jawafdehi_shared.auth.oidc import OIDCAuthentication  # noqa: E402
from rest_framework.authentication import SessionAuthentication  # noqa: E402
from rest_framework.permissions import AllowAny  # noqa: E402


class DevAwareSessionAuthentication(SessionAuthentication):
    """SessionAuthentication that is INERT unless DEV_AUTH is on, evaluated per
    request. DRF freezes a view's authenticators from DEFAULT_AUTHENTICATION_CLASSES
    at import time (APIView.authentication_classes is a class attr set once), so a
    view that must accept the DEV_AUTH session — like ``me`` and the dev-login
    endpoints — can't rely on the global list, which only includes Session when
    DEV_AUTH was truthy at settings-load. Pinning this class on those views makes
    the session path work whenever DEV_AUTH is on, regardless of load order, and
    stay OIDC-only in production (where DEV_AUTH is always False)."""

    def authenticate(self, request):
        if not settings.DEV_AUTH:
            return None
        return super().authenticate(request)


# The authenticators for endpoints the SPA reaches with EITHER an OIDC bearer or
# (in dev) a Django session: OIDC first, session second (inert unless DEV_AUTH).
DEV_OR_OIDC_AUTH = [OIDCAuthentication, DevAwareSessionAuthentication]


@api_view(["GET"])
@authentication_classes(DEV_OR_OIDC_AUTH)
@permission_classes([CanReadReview])
def me_view(request):
    """Return the signed-in user + their roles (for the SPA header / gating).

    Accepts an OIDC bearer or (in dev) the session opened by dev_login_view —
    authenticators are pinned so the dev session works regardless of DRF's
    import-time freezing of the global default auth classes.
    """
    return Response(_user_roles_payload(request.user))


@api_view(["POST"])
@authentication_classes([DevAwareSessionAuthentication])
@permission_classes([AllowAny])
def dev_login_view(request):
    """DEV-ONLY: authenticate with username/password, open a Django session.

    Returns the same {username, roles, is_admin} shape as ``me`` plus a CSRF
    token the SPA must echo as X-CSRFToken on subsequent session-authenticated
    writes. Hard 404 when DEV_AUTH is off so it can never exist in production.
    """
    if not settings.DEV_AUTH:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    # request.data is a list/scalar for a non-object body — guard so .get() can't
    # raise AttributeError → 500.
    if not isinstance(request.data, dict):
        return Response(
            {"detail": "Request body must be a JSON object."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    username = (request.data.get("username") or "").strip()
    password = request.data.get("password") or ""
    user = authenticate(request, username=username, password=password)
    if user is None:
        return Response(
            {"detail": "Invalid username or password."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    login(request, user)
    payload = _user_roles_payload(user)
    payload["csrftoken"] = get_token(request)
    return Response(payload)


@api_view(["POST"])
@authentication_classes([DevAwareSessionAuthentication])
@permission_classes([AllowAny])
def dev_logout_view(request):
    """DEV-ONLY: end the Django session opened by dev_login_view."""
    if not settings.DEV_AUTH:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    logout(request)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["POST"])
@permission_classes([HasContributorRole])
def submit_review(request):
    """Submit a new case slug for review.

    Creates the review as `pending` and enqueues a ``case_review`` job on the
    central queue (``jobs`` app). The out-of-process poller — now a generic jobs
    consumer — claims it via ``/api/jobs/claim`` and runs it. Nothing executes
    in-process here. The queue owns claim/lease/retry; this view only enqueues.
    """
    s = SubmitSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    slug = s.validated_data["slug"]
    review = CaseReview.objects.create(slug=slug, submitted_by=request.user)
    _enqueue_review_job(review, submitted_by=request.user)
    return Response(
        CaseReviewDetailSerializer(review).data, status=status.HTTP_201_CREATED
    )


def _enqueue_review_job(review, *, submitted_by=None):
    """Enqueue (or re-enqueue) the ``case_review`` job backing one review row.

    ``dedup_key`` is the review id, so a review that is already queued/running
    is never double-enqueued; once its prior job is terminal the key frees and a
    regrade can enqueue afresh. The case dict is resolved SERVER-SIDE at claim
    time by the kind's ``build_payload`` hook, so the poller stays DB-free.

    Enqueuing also SUPERSEDES any older still-queued job for the same slug
    (dead-lettered; its review is finalized as failed/"superseded"): the case is
    resolved live at claim time, so a second queued job for the same slug would
    just re-grade identical content at full LLM cost.
    """
    from django.db import transaction

    from jobs import queue as job_queue

    from .supersede import supersede_older_queued_jobs

    # One transaction for enqueue + supersede: without it the new job commits
    # first and a consumer polling in the gap can claim the older duplicate
    # (claim orders by id, so it picks the older one) before it is dead-lettered.
    with transaction.atomic():
        job = job_queue.enqueue(
            "case_review",
            payload={"slug": review.slug, "review_id": review.id},
            dedup_key=f"case_review:{review.id}",
            submitted_by=submitted_by,
        )
        supersede_older_queued_jobs(review.slug, keep_job_id=job.pk)
    return job


# ---------------- Job API — RETIRED (moved to the central `jobs` app) --------
# The review app is no longer its own queue. The DB-free poller is now a generic
# jobs consumer that speaks the central protocol at:
#     POST /api/jobs/claim/          (kinds=["case_review"])
#     POST /api/jobs/<id>/stage/
#     POST /api/jobs/<id>/result/
# The pieces that used to live here as claim_job / job_stage / submit_job_result
# now live as the ``case_review`` KindSpec hooks in ``jobs/consumers.py``:
#   - build_payload  -> resolves the case dict + config server-side at claim time
#                       (the poller stays DB-free), replacing claim_job's body.
#   - on_result      -> finalizes the CaseReview row from the scored result,
#                       replacing submit_job_result's success branch.
#   - on_failure     -> marks the CaseReview failed on terminal job failure.
# The queue itself owns the atomic claim (select_for_update skip_locked), the
# stale-result 409 guard, leases, retries, and dedup. See docs/jobs-queue-design.md.


class ReviewListView(generics.ListAPIView):
    serializer_class = CaseReviewListSerializer
    permission_classes = [CanReadReview]

    def get_queryset(self):
        return CaseReview.objects.all()


class GroupedReviewListView(generics.ListAPIView):
    """GET /api/casework/reviews/grouped/

    The flat review list carries one row per execution; the SPA's review list
    page instead wants ONE entry per case with ALL of that case's executions
    (so an older run doesn't fall onto a later page of the flat list). This view
    groups CaseReview rows by case slug and paginates BY CASE.

    Each result: {slug, case_title, latest: <ReviewListItem>,
    executions: [<ReviewListItem> ...]} — executions newest-first, cases ordered
    by their most-recent execution (newest case first). ``latest`` is
    ``executions[0]``. Uses the same CaseReviewListSerializer as the flat list
    so item shapes match exactly.
    """

    permission_classes = [CanReadReview]
    serializer_class = CaseReviewListSerializer

    def list(self, request, *args, **kwargs):
        # Paginate BY CASE at the DB level: rank slugs by their most-recent
        # execution, then fetch only the current page's rows — instead of
        # loading the whole CaseReview table into memory to group in Python.
        slug_qs = (
            CaseReview.objects.values("slug")
            .annotate(latest_created_at=Max("created_at"))
            .order_by("-latest_created_at")
            .values_list("slug", flat=True)
        )

        page = self.paginate_queryset(slug_qs)
        page_slugs = list(page) if page is not None else list(slug_qs)

        # Fetch executions only for this page's slugs. Meta.ordering (-created_at)
        # gives newest-first, so grouping in iteration order keeps each case's
        # executions newest-first.
        groups: dict[str, list[CaseReview]] = {}
        for review in CaseReview.objects.filter(slug__in=page_slugs):
            groups.setdefault(review.slug, []).append(review)

        results = []
        for slug in page_slugs:  # preserves the DB-ranked newest-case-first order
            executions = groups.get(slug)
            if not executions:  # defensive: a slug vanished between the two queries
                continue
            items = CaseReviewListSerializer(executions, many=True).data
            latest = executions[0]
            results.append(
                {
                    "slug": slug,
                    # The case title is snapshotted on every review row; the
                    # newest execution carries the freshest value.
                    "case_title": latest.case_title,
                    "latest": items[0],
                    "executions": items,
                }
            )

        if page is not None:
            return self.get_paginated_response(results)
        return Response(results)


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

    Any caseworker may read the config, but only Admin / Moderator may change
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
    """Re-queue every distinct CASE for regrading against the current rules.

    One regrade per slug — the LATEST review row of each slug is reset to
    pending and a fresh ``case_review`` job is enqueued on the central queue;
    the out-of-process consumer then claims and runs them. Older review rows of
    the same slug are history and stay untouched: a review grades the LIVE case
    (resolved at claim time), so re-running every historical row would grade the
    same content N times at full LLM cost — that is exactly how the queue once
    accumulated hundreds of duplicate jobs over a few dozen cases.
    """
    # Only id + slug are needed to reset + enqueue; avoid loading the (large)
    # result JSON for every review.
    # .order_by() clears CaseReview's Meta.ordering for explicitness. Django
    # >= 3.0 ignores Meta.ordering when grouping, so this is belt-and-braces —
    # the grouping-by-slug is covered by test_regrade_all_targets_only_the_
    # latest_review_per_slug either way.
    latest_ids = (
        CaseReview.objects.values("slug")
        .annotate(latest_id=Max("id"))
        .order_by()
        .values_list("latest_id", flat=True)
    )
    reviews = list(CaseReview.objects.only("id", "slug").filter(id__in=latest_ids))
    CaseReview.objects.filter(id__in=[r.id for r in reviews]).update(
        status=CaseReview.STATUS_PENDING,
        stage="queued_for_regrade",
        error="",
        updated_at=timezone.now(),
    )
    for review in reviews:
        _enqueue_review_job(review, submitted_by=request.user)
    return Response({"regrading": len(reviews), "review_ids": [r.id for r in reviews]})
