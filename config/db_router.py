"""Primary/replica database routing for the read/write split.

The CloudNativePG Postgres cluster exposes a read-write endpoint (``pg-rw``,
the primary) and a read endpoint (``pg-r``, primary + standbys). The ORM
``default`` / ``ngm`` aliases target the write endpoints; ``replica`` /
``ngm_replica`` target ``pg-r``. This router sends ORM reads to the replica
aliases and writes to the primaries, and keeps migrations off the replicas.

``pg-r`` load-balances across asynchronous standbys, so a read issued right
after a write may not observe that write. Call sites needing read-your-writes
consistency run inside :func:`force_primary_reads` (the
:class:`ForcePrimaryReadsMiddleware` does this for the Django admin and for
every unsafe-method request).
"""

import functools
from contextlib import contextmanager
from contextvars import ContextVar

from django.conf import settings

# Primary write alias -> its read-replica alias.
PRIMARY_TO_REPLICA = {
    "default": "replica",
    "ngm": "ngm_replica",
}
REPLICA_TO_PRIMARY = {
    replica: primary for primary, replica in PRIMARY_TO_REPLICA.items()
}
READ_ALIASES = frozenset(PRIMARY_TO_REPLICA.values())

# ContextVar (not threading.local) so the flag is isolated correctly under both
# sync WSGI threads and async/ASGI tasks. A freshly spawned thread starts from
# the default (False), so a management command's force-primary scope never leaks
# into runserver's request-handling threads.
_force_primary_var: ContextVar[bool] = ContextVar("force_primary", default=False)


def _force_primary() -> bool:
    return _force_primary_var.get()


@contextmanager
def force_primary_reads():
    """Pin ORM reads to the primary for the duration of the block.

    Use around read-after-write sequences that must observe their own writes
    (the async standby behind ``pg-r`` can otherwise serve a stale snapshot).
    """
    token = _force_primary_var.set(True)
    try:
        yield
    finally:
        _force_primary_var.reset(token)


def install_management_command_primary_reads() -> None:
    """Pin reads to the primary for the duration of every management command.

    Management commands are not request-scoped, so ``ForcePrimaryReadsMiddleware``
    cannot cover their read-modify-write sequences. Without this, once
    ``DATABASE_READ_URL`` is set a command's ORM reads would hit the lagging
    ``pg-r`` standby — e.g. a ``get_or_create`` that reads "absent" from a stale
    replica and then creates a duplicate on the primary, or a ``select_for_update``
    routed to a connection outside its own transaction. Wrapping
    ``BaseCommand.execute`` keeps offline jobs (data imports, enrichment, merges)
    reading their own writes; web GET traffic still uses the replica.

    Long-running servers are unaffected: ``runserver`` serves requests in worker
    threads that start from the ContextVar default (replica), and prod web runs
    through WSGI/ASGI, not ``BaseCommand.execute``. Idempotent — safe to call from
    multiple AppConfigs.
    """
    from django.core.management.base import BaseCommand

    if getattr(BaseCommand, "_primary_reads_patched", False):
        return

    original_execute = BaseCommand.execute

    @functools.wraps(original_execute)
    def execute(self, *args, **kwargs):
        with force_primary_reads():
            return original_execute(self, *args, **kwargs)

    BaseCommand.execute = execute
    BaseCommand._primary_reads_patched = True


def _canonical(db: str) -> str:
    """Collapse a replica alias onto its primary so a pair can be compared."""
    return REPLICA_TO_PRIMARY.get(db, db)


class PrimaryReplicaRouter:
    """Route reads to ``replica`` and writes to ``default`` (primary)."""

    def db_for_read(self, model, **hints):
        # Fall back to the primary when no distinct replica is configured
        # (dev/CI/single-endpoint) or when a read-your-writes block is active.
        if _force_primary() or "replica" not in settings.DATABASES:
            return "default"
        return "replica"

    def db_for_write(self, model, **hints):
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        if _canonical(obj1._state.db) == _canonical(obj2._state.db):
            return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Schema changes only run against the primary write aliases.
        if db in READ_ALIASES:
            return False
        return None
