"""Worker-side handler for the ``newsletter_sendpulse`` job kind.

This runs in the JOBS CONSUMER process (``review_poller --kinds
newsletter_sendpulse``), NOT in the API. The subscription's current state is
resolved server-side at claim time into ``payload['subscription']`` (by the
kind's ``build_payload`` hook in ``jobs.consumers``); the handler pushes it to
SendPulse and returns the resulting sync status. The server's ``on_result`` hook
persists that status back onto the ``NewsletterSubscription`` row, so the handler
itself stays database-free.

Handler signature (the poller's contract):
    handler(payload: dict, *, on_stage: Callable[[str], None]) -> dict
"""

from __future__ import annotations

from typing import Callable


def handle_newsletter_sendpulse(
    payload: dict,
    *,
    on_stage: Callable[[str], None],
) -> dict:
    """Push the subscription's state to SendPulse → ``{"sync_status": ...}``.

    Raises on provider/config errors (a retryable failure) so the queue re-queues
    with backoff up to the kind's ``max_attempts``, then dead-letters.
    """
    # Local import: keeps the API process from pulling the service in via this
    # module and mirrors materials.job_handlers' lazy-import convention.
    from cases.services.sendpulse import push_subscription

    data = payload.get("subscription")
    if not data:
        raise ValueError("newsletter_sendpulse payload has no 'subscription'.")

    on_stage("sendpulse:push")
    sync_status = push_subscription(data)
    on_stage(f"sendpulse:{sync_status}")
    return {"sync_status": sync_status}


#: kind -> worker-side handler, merged into the poller's HANDLERS registry.
HANDLERS = {"newsletter_sendpulse": handle_newsletter_sendpulse}
