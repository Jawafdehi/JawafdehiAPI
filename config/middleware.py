"""Request middleware that steers anonymous public reads to the DB read replica.

The DB router (config.db_router) can send reads to a per-service read replica, but
only when this middleware flags the request as replica-eligible. The decision is
made from the request PATH and METHOD, deliberately NOT from the authenticated
user: DRF/OIDC resolves the user inside the view, which is too late for a
middleware auth check. So:

* unsafe methods (writes) → primary (read-your-write within the request), and
* admin / casework / ingestion / OIDC surfaces → primary (editors must always see
  their own just-saved data),
* everything else — anonymous public GETs (cases, entities, court cases, materials,
  search, sitemaps) — → replica.

The flag is always reset in ``finally`` so a pooled worker thread never leaks it
into the next request. A no-op when no replica is configured (REPLICA_ALIASES
empty) since the router then falls back to the primary regardless.
"""

from __future__ import annotations

from config.db_router import route_reads_to_replica

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Paths that must always read from the primary (they either write, or are editor
# surfaces where stale replica reads would be confusing).
_PRIMARY_ONLY_PREFIXES = (
    "/django-admin/",
    "/api/casework",
    "/api/caseworker",
    "/api/jobs/",
    "/api/ingestion/",
    "/newsroom/",
    "/oidc/",
)


class ReadReplicaRoutingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        use_replica = request.method in _SAFE_METHODS and not request.path.startswith(
            _PRIMARY_ONLY_PREFIXES
        )
        route_reads_to_replica(use_replica)
        try:
            return self.get_response(request)
        finally:
            route_reads_to_replica(False)
