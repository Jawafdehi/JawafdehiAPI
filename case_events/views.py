# SPDX-License-Identifier: Hippocratic-3.0
"""The one producer a human drives: a caseworker's manual note.

Every other producer watches something. This one is typed. A caseworker who
learns a fact — the court published an order today, an appeal was filed, a name
in the record is wrong — writes it in a sentence and the pipeline drafts a
properly-shaped timeline entry for them to approve or discard.

Two reasons it earns its place rather than being a shortcut around the SPA's
existing case editor:

**It is fast capture.** Writing "SC admitted the appeal on 2082-11-20, per
kathmandupost.com/…" is a few seconds; opening the case editor, finding the
timeline, and composing a well-formed dated entry in Nepali is not. The model
does the shaping and the caseworker judges the result, which is the division of
labour the whole system is built around.

**It is the only producer that can be fired on demand**, which makes it the
acceptance test for the bus itself. Post one note and every hop — matcher,
proposal-builder, the intent job, the proposal, the notifier — either works or
tells you where it stopped, without waiting for a scrape.

It stays a SIGNAL rather than writing a proposal directly. A caseworker note is
an observed fact like any other, and routing it down the same path means it gets
the same duplicate detection, the same audit trail, and the same "check the
timeline before proposing" pass the model applies to scraped facts.
"""

from __future__ import annotations

import structlog
from django.conf import settings
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from case_events import subjects
from case_events.producers import emit
from review.permissions import IsContentStaff

logger = structlog.get_logger(__name__)

PRODUCER = "producer:caseworker"


class ManualNoteSerializer(serializers.Serializer):
    """A caseworker's free-text observation about one case."""

    case_slug = serializers.SlugField(max_length=50)
    note = serializers.CharField(max_length=4000)
    #: Where the caseworker got it. Not required, but every factual claim in the
    #: archive is supposed to carry one, and a note without a source produces a
    #: proposal a reviewer cannot check.
    source = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    #: When the FACT happened, if known — not when the note was typed.
    occurred_at = serializers.DateTimeField(required=False, allow_null=True, default=None)

    def validate_case_slug(self, value):
        """The case must exist, and the slug must build a canonical ``@id``.

        Checked here rather than left to the matcher: a typo'd slug would
        otherwise be accepted, published, matched to nothing, and silently
        dropped — with the caseworker told it worked.
        """
        from cases.models import Case
        from jawafdehi_shared.entities.ids import build_case_iri

        try:
            build_case_iri(value)
        except Exception as exc:
            raise serializers.ValidationError(f"{value!r} is not a valid case @id segment: {exc}") from None
        if not Case.objects.filter(slug=value).exists():
            raise serializers.ValidationError(f"No case with slug {value!r}.")
        return value

    def validate_note(self, value):
        if not value.strip():
            raise serializers.ValidationError("A note cannot be blank.")
        return value.strip()


class ManualNoteView(APIView):
    """``POST /api/signals/manual-note/`` — put a caseworker observation on the bus.

    Gated to content staff, NOT to the contributor role that may create
    proposals. A note here becomes a model call and then a queue item for a
    human; the automation identity must not be able to generate its own work.
    """

    permission_classes = [IsContentStaff]

    def post(self, request):
        serializer = ManualNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if not getattr(settings, "NATS_URL", ""):
            # 503 rather than a cheerful 202. Publishing no-ops without a broker,
            # and telling a caseworker their note was accepted when it went
            # nowhere is the worst available outcome.
            return Response(
                {"detail": "The event bus is not configured, so this note would go nowhere."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        from jawafdehi_shared.entities.ids import build_case_iri

        case_iri = build_case_iri(data["case_slug"])
        actor = self._actor(request)
        # Deterministic in the fact, not the moment: the same note about the
        # same case is a duplicate however many times it is filed. Note that the
        # key does NOT include the caseworker, so two people who independently
        # notice the same thing collapse to one proposal — which is the point,
        # and worth stating because it is not what "who filed it" would suggest.
        # Hashed because a note is long and free-form, and dedup_key is bounded.
        import hashlib

        digest = hashlib.sha256(data["note"].encode("utf-8")).hexdigest()[:32]
        dedup_key = f"manual:{data['case_slug']}:{digest}"

        sent = emit(
            subjects.SIGNAL_MANUAL_NOTE,
            producer=PRODUCER,
            payload={
                "case_slug": data["case_slug"],
                "note": data["note"],
                "filed_by": actor,
            },
            # The case @id, so the matcher treats this as an assertion rather
            # than an inference — the caseworker has told us which case it is.
            subject_refs=[case_iri],
            dedup_key=dedup_key,
            source=data["source"] or "caseworker",
            occurred_at=data["occurred_at"],
            # Wait for the broker's answer before telling a human it worked.
            # Fire-and-forget returns True the moment the publish is scheduled,
            # so a 202 would have been indistinguishable from a broker that
            # rejected every message — which is exactly what a fresh one does
            # until `nats_bootstrap` has run. This endpoint doubles as the
            # bus's acceptance test; a green light it cannot back up is worse
            # than no endpoint at all. Costs one round trip on a hand-typed note.
            wait=True,
        )

        logger.info(
            "case_events.manual_note_filed",
            case_slug=data["case_slug"],
            filed_by=actor,
            published=sent,
        )

        if not sent:
            return Response(
                {"detail": "The note could not be published to the event bus."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(
            {
                "subject": subjects.SIGNAL_MANUAL_NOTE,
                "dedup_key": dedup_key,
                "case_slug": data["case_slug"],
                # Said plainly, because the useful thing to know is that nothing
                # has happened to the case yet.
                "detail": (
                    "Filed. A proposal will appear in the review queue if the note "
                    "warrants one; nothing is written to the case until you approve it."
                ),
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @staticmethod
    def _actor(request) -> str:
        user = request.user
        handle = getattr(user, "username", "") or getattr(user, "email", "") or str(user.pk)
        return f"caseworker:{handle}"
