"""Helpers for recording audit-log entries on writes that bypass model signals.

django-auditlog hooks Django's ``post_save``/``post_delete`` signals, so any
mutation performed with a bulk queryset ``.update()`` (or ``QuerySet.update()``
on a single row) is invisible to it. A few code paths deliberately use bulk
``.update()`` — e.g. to persist a single column without running the model's
full ``full_clean()`` over an otherwise-invalid row. For those, call
:func:`log_field_update` to record an equivalent ``LogEntry`` manually.

The actor is filled in automatically by auditlog's ``set_actor`` context
(installed by the request middleware) via the ``LogEntry`` ``pre_save`` signal,
so callers do not pass the user explicitly.
"""

from auditlog.models import LogEntry


def log_field_update(instance, changes: dict) -> None:
    """Record a manual UPDATE LogEntry for ``instance``.

    :param instance: the saved model instance (already persisted).
    :param changes: mapping of ``field_name -> [old_value, new_value]``.
        Skipped silently when empty so callers can pass a computed diff.
    """
    if not changes:
        return

    LogEntry.objects.log_create(
        instance,
        action=LogEntry.Action.UPDATE,
        changes=changes,
    )
