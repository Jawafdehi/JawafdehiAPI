"""Casework Review System API views (ported into jawafdehi-api).

Auth model change vs. the standalone casework system:
  - The standalone app issued its own DRF auth token via /auth/login/.
  - Here we reuse jawafdehi-api's JWT. Clients obtain a token pair from the
    existing /api/caseworker/auth/token/ endpoint (SimpleJWT TokenObtainPair)
    and send it as `Authorization: Bearer <access>`.
  - Every endpoint additionally requires the Contributor role (HasContributorRole).

So there is no login_view here anymore; the SPA logs in against the shared JWT
endpoint. We expose a small `me` view so the SPA can show who is signed in and
gate the UI by role.
"""

from django.utils import timezone
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from . import code_rules, pipeline
from .models import CaseReview, ReviewConfig
from .permissions import HasContributorRole, IsAdminOrModerator
from .serializers import (
    CaseReviewDetailSerializer,
    CaseReviewListSerializer,
    ReviewConfigSerializer,
    SubmitSerializer,
)


@api_view(["GET"])
@permission_classes([HasContributorRole])
def me_view(request):
    """Return the signed-in user + their roles (for the SPA header / gating)."""
    user = request.user
    roles = list(user.groups.values_list("name", flat=True))
    if user.is_superuser and "Admin" not in roles:
        roles = ["Admin"] + roles
    return Response(
        {
            "username": user.username,
            "roles": roles,
            "is_admin": user.is_superuser or "Admin" in roles,
        }
    )


@api_view(["POST"])
@permission_classes([HasContributorRole])
def submit_review(request):
    """Submit a new case slug for review; kicks off the async pipeline."""
    s = SubmitSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    slug = s.validated_data["slug"]
    review = CaseReview.objects.create(slug=slug, submitted_by=request.user)
    pipeline.run_review_async(review.id)
    return Response(
        CaseReviewDetailSerializer(review).data, status=status.HTTP_201_CREATED
    )


class ReviewListView(generics.ListAPIView):
    serializer_class = CaseReviewListSerializer
    permission_classes = [HasContributorRole]

    def get_queryset(self):
        return CaseReview.objects.all()


class ReviewDetailView(generics.RetrieveAPIView):
    serializer_class = CaseReviewDetailSerializer
    permission_classes = [HasContributorRole]
    queryset = CaseReview.objects.all()


# ---------------- Rules (code-enforced, read-only) ----------------
# Rules now live entirely in code (review/rule_defaults.py via code_rules.py).
# They are surfaced read-only for display; there is no create/edit/delete/reset.


@api_view(["GET"])
@permission_classes([HasContributorRole])
def rules_list(request):
    """List all (code-defined) rules, in display order."""
    return Response([code_rules.as_dict(r) for r in code_rules.get_rules()])


@api_view(["GET"])
@permission_classes([HasContributorRole])
def rule_detail(request, pk):
    """Retrieve a single (code-defined) rule by its stable id."""
    rule = code_rules.get_rule(pk)
    if rule is None:
        return Response({"detail": "Rule not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(code_rules.as_dict(rule))


@api_view(["GET", "PUT"])
@permission_classes([HasContributorRole])
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
    """Re-queue every existing review for regrading against the current rules.

    Each review is reset to pending in a single bulk UPDATE; the dispatcher then
    picks them up (each review fans its LLM rules out in parallel).
    """
    ids = list(CaseReview.objects.values_list("id", flat=True))
    CaseReview.objects.filter(id__in=ids).update(
        status=CaseReview.STATUS_PENDING,
        stage="queued_for_regrade",
        error="",
        updated_at=timezone.now(),
    )
    pipeline.run_many_async(ids)
    return Response({"regrading": len(ids), "review_ids": ids})
