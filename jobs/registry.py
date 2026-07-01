"""Per-kind configuration registry for the job queue.

Each job ``kind`` registers a :class:`KindSpec` describing its queue policy
(lease duration, retry budget) and, optionally, server-side hooks:

- ``build_payload(job)`` — enrich the claimed job's payload with data resolved
  SERVER-SIDE at claim time, so a DB-free consumer needs no direct DB access
  (this is how the review kind hands the poller a fully-resolved case dict).
- ``on_result(job, data)`` — apply a successful result to the kind's own domain
  record (e.g. finalize the ``CaseReview`` row from the job result).

The jobs app stays domain-agnostic: kinds are registered by the owning app
(``review``, ``materials``, …) at import time via ``jobs.consumers`` hooks that
this module imports. Registration is idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Optional

#: Fallbacks when a kind doesn't override them.
DEFAULT_LEASE_SECONDS = 600  # 10 min
DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class KindSpec:
    """Queue policy + optional server-side hooks for one job ``kind``."""

    kind: str
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    #: Resolve extra payload for a just-claimed job (server-side). Returns a dict
    #: merged into ``job.payload``, or None. May raise to fail the claim.
    build_payload: Optional[Callable[[Any], Optional[dict]]] = None
    #: Apply a successful handler result to the kind's domain record.
    on_result: Optional[Callable[[Any, dict], None]] = None
    #: Apply a TERMINAL failure (FAILED/DEAD) to the kind's domain record. Not
    #: called on a retry re-queue — only when the job is finally given up on.
    on_failure: Optional[Callable[[Any], None]] = None

    @property
    def lease(self) -> timedelta:
        return timedelta(seconds=self.lease_seconds)


_REGISTRY: dict[str, KindSpec] = {}


def register(spec: KindSpec) -> None:
    """Register (or replace) the spec for ``spec.kind``. Idempotent."""
    _REGISTRY[spec.kind] = spec


def get(kind: str) -> KindSpec:
    """Return the spec for ``kind``, or a default spec if unregistered.

    An unregistered kind is still a valid queue citizen (it just uses the
    default lease/retry policy and has no server-side hooks) — this keeps the
    queue usable for ad-hoc kinds without forcing a registration.
    """
    return _REGISTRY.get(kind) or KindSpec(kind=kind)


def known_kinds() -> list[str]:
    return sorted(_REGISTRY)


# Import consumer registrations so kinds are known once this module is imported
# (jobs.apps.ready imports this module). Guarded so a consumer import error
# never breaks the queue core itself.
try:  # pragma: no cover - defensive import wiring
    from . import consumers  # noqa: F401
except Exception:  # noqa: BLE001
    pass
