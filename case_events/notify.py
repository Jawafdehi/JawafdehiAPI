# SPDX-License-Identifier: Hippocratic-3.0
"""Announcing a proposal decision to an outbound webhook.

Separate from :mod:`case_events.consumers.handlers` because the interesting parts
here are about *what may leave the building*, not about consuming a message.

**The payload deliberately omits the drafted intent.** A PENDING proposal is
unreviewed model output about named individuals in corruption cases, and the whole
point of the review queue is that nothing is published until a human agrees with
it. Posting the draft text to an external chat service would publish it — to a
third party, in a channel with its own retention, before anyone has checked it. So
the notification says *that* a proposal needs review and *where to find it*, and
the reviewer reads the content in the app behind SSO. This is a smaller message
and a correct one, not a compromise.

**Failures never propagate.** The proposal row already exists and the queue UI is
the primary surface; a webhook that is down must not cause a JetStream redelivery,
because redelivery would re-notify rather than re-do anything useful. Every failure
mode logs and returns False.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)

#: What the reviewer is being pointed at. Kept here rather than built from a
#: request, because a consumer has no request to build one from.
QUEUE_PATH = "/admin/proposals"

#: U+200B. Named and escaped so it is visible in source — see
#: :func:`defang_mentions`.
ZERO_WIDTH_SPACE = "\u200b"


def _base_url() -> str:
    configured = (getattr(settings, "FRONTEND_BASE_URL", "") or "").rstrip("/")
    return configured or "https://jawafdehi.org"


def defang_mentions(text: str) -> str:
    """Make ``@`` in interpolated text unable to ping anyone.

    Found in review, and it is a gap the module docstring did not cover: that
    docstring bounds what the payload CONTAINS, and this is about what it can DO.
    Two of the interpolated fields are human-authored — ``case_slug`` derives from
    a title a caseworker wrote, and ``reviewer`` is an account handle — so
    ``@everyone`` reaching the rendered line would ping an entire server from a
    case record. That is not a data leak, it is worse in one respect: it is a
    notification channel becoming a nuisance channel, which gets it muted, which
    silently ends the notifications.

    A zero-width space after the ``@`` is the standard trick: it renders
    indistinguishably and stops the mention resolving.

    Written as the ``\\u200b`` ESCAPE rather than the character itself, on purpose.
    A literal zero-width space in source is invisible: it can be deleted by an
    editor, a reformat or a careless selection with nothing looking wrong
    afterwards, and the test for this would then be the only sign left.
    """
    return text.replace("@", f"@{ZERO_WIDTH_SPACE}")


def build_message(payload: dict) -> dict:
    """The body posted to the webhook.

    Carries both a rendered ``content`` line (Discord and most chat receivers
    require one) and the structured fields beside it, so a different receiver does
    not have to parse the prose back out.
    """
    status = payload.get("status") or "updated"
    case_slug = payload.get("case_slug") or "unknown-case"
    proposal_id = payload.get("proposal_id")
    confidence = payload.get("confidence")
    reviewer = payload.get("reviewer")

    link = f"{_base_url()}{QUEUE_PATH}"
    where = f"#{proposal_id}" if proposal_id else "(id unknown)"
    tail = f" by {reviewer}" if reviewer else ""
    confidence_note = f", confidence {confidence}" if confidence is not None else ""

    content = defang_mentions(
        f"Case update proposal {where} is **{status}**{tail} "
        f"on `{case_slug}`{confidence_note}. Review: {link}"
    )

    return {
        # No case title and no drafted text — see the module docstring.
        "content": content,
        # Belt to the escaping's braces, and the half that actually binds a Discord
        # receiver: with `parse` empty, even an unescaped mention is not resolved.
        # Harmless to any other receiver, which ignores a field it does not know.
        "allowed_mentions": {"parse": []},
        # NOT defanged. The structured half is for machines, and mangling an
        # identifier a receiver might match on would be worse than the risk this
        # guards. Only the prose that gets RENDERED is escaped.
        "proposal_id": proposal_id,
        "case_slug": case_slug,
        "status": status,
        "confidence": confidence,
        "reviewer": reviewer,
        "url": link,
    }


def post(payload: dict) -> bool:
    """Announce one proposal transition. Returns whether it was delivered.

    Never raises. A notification is the least important thing on this thread.
    """
    url = getattr(settings, "CASE_EVENTS_WEBHOOK_URL", "") or ""
    if not url:
        # Not a warning. Log-only is the documented default, and a warning per
        # proposal would train people to ignore the log this consumer exists for.
        logger.debug("case_events.webhook_not_configured")
        return False

    body = json.dumps(build_message(payload)).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "jawafdehi-case-events/1.0"},
        method="POST",
    )
    timeout = float(getattr(settings, "CASE_EVENTS_WEBHOOK_TIMEOUT", 5))

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # 2xx. Discord answers 204 with no body; treat any 2xx as delivered
            # rather than pinning one code a different receiver would not use.
            delivered = 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        # The endpoint answered and refused. Worth a warning: a rotated or revoked
        # webhook fails this way forever and silently, and the URL must NOT be
        # logged — it is the credential.
        logger.warning("case_events.webhook_rejected", status=exc.code, reason=str(exc.reason)[:200])
        return False
    except Exception as exc:  # noqa: BLE001 - timeouts, DNS, TLS, malformed URL
        logger.warning("case_events.webhook_failed", error=str(exc)[:200])
        return False

    if not delivered:
        logger.warning("case_events.webhook_unexpected_status", status=response.status)
    return delivered
