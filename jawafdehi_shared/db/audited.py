"""ORM-level audit capture for ``QuerySet.update()`` / ``bulk_update()``.

django-auditlog hooks ``post_save`` / ``pre_save`` / ``post_delete``. A bulk
``QuerySet.update()`` emits none of those, so those edits — which is how most
data is actually changed on this platform (interactive pod-ORM shells,
management commands, backfills) — leave no trail. ``QuerySet.delete()`` already
logs: a registered ``post_delete`` receiver disables Django's fast-delete path
so per-row ``post_delete`` fires. This module therefore deliberately does NOT
touch ``delete()`` — only ``update()`` / ``bulk_update()``.

Register a model with :func:`register_audited` (from its ``AppConfig.ready()``)
to both register it with auditlog and swap its manager for one whose queryset
mixes in :class:`AuditedQuerySet`. Its overrides write the same UPDATE
``LogEntry`` rows a per-instance ``save()`` would, honoring the model's
registered field include/exclude/mask config (``model_instance_diff`` reads the
registry). Actor /
remote_addr / cid ride the ambient auditlog context (see
:class:`jawafdehi_shared.drf.auditlog.AuditlogActorMixin`), so request-driven
bulk writes attribute to the user and out-of-band ones log ``actor=NULL``
(honest — there is no request).

Rows are diffed individually up to ``AUDIT_BULK_UPDATE_MAX_ROWS``; a larger
write records a single summary entry (row count + fields) instead, so a big
backfill can neither explode the ``LogEntry`` table nor pay an O(rows) diff cost.
The read used to snapshot the pre-/post-image is pinned to the WRITE database
(never a read replica) so it always sees its own write and so ``nes`` / ``ngm``
models — whose ``LogEntry`` rows are cross-DB writes to ``default`` — snapshot
from the right connection.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models, router

from auditlog import get_logentry_model
from auditlog.cid import get_cid
from auditlog.context import auditlog_disabled
from auditlog.registry import auditlog

from jawafdehi_shared.drf.auditlog import log_bulk_update

#: Fallback when ``AUDITLOG`` settings do not pin a threshold. Above this many
#: affected rows a single summary entry is written instead of one-per-row.
DEFAULT_BULK_UPDATE_MAX_ROWS = 1000


def _max_rows() -> int:
    return getattr(
        settings, "AUDIT_BULK_UPDATE_MAX_ROWS", DEFAULT_BULK_UPDATE_MAX_ROWS
    )


def _logging_disabled() -> bool:
    """Mirror auditlog's own ``check_disable`` gate for the signal path."""
    try:
        return auditlog_disabled.get()
    except LookupError:
        return False


def _auditable_fields(model, fields):
    """Drop ``auto_now`` touch columns from a set of updated field names.

    ``updated_at``-style ``auto_now`` timestamps are bumped on essentially every
    write (the DRF write paths set them explicitly on ``update()`` since
    ``auto_now`` does not fire for a queryset update), so a change to one of them
    alone is not a substantive edit. Ignoring them keeps a pure touch — and a
    no-op content PATCH that only refreshes the timestamp — from manufacturing a
    spurious audit entry, and strips timestamp noise from real diffs.
    """
    auto_now = {f.name for f in model._meta.fields if getattr(f, "auto_now", False)}
    return [f for f in fields if f not in auto_now]


def _log_bulk_summary(model, count, fields):
    """Write one summary ``LogEntry`` for an update too large to diff per row.

    Not anchored to a single object (there are many) — ``object_pk`` is left
    blank and the payload records the affected row count and field names so the
    trail still shows *that* a mass edit happened, by whom and when, without the
    O(rows) cost or table growth of per-row entries.
    """
    # Imported lazily: this module is loaded from AppConfig.ready(), and keeping
    # the ContentType model import out of module scope avoids any app-registry
    # ordering surprise if it is ever imported earlier.
    from django.contrib.contenttypes.models import ContentType

    log_entry_model = get_logentry_model()
    return log_entry_model.objects.create(
        content_type=ContentType.objects.get_for_model(model),
        object_pk="",
        object_repr=f"bulk update: {count} {model._meta.label} row(s)",
        action=log_entry_model.Action.UPDATE,
        changes={
            "__bulk_update__": {"rows": count, "fields": sorted(fields)},
        },
        cid=get_cid(),
    )


class AuditedQuerySet(models.QuerySet):
    """QuerySet that logs ``update()`` / ``bulk_update()`` to auditlog.

    Only acts for models registered with auditlog; for anything else (or when
    auditing is disabled in the current context) it is a transparent
    passthrough.
    """

    def update(self, **kwargs):
        model = self.model
        if _logging_disabled() or not kwargs or not auditlog.contains(model):
            return super().update(**kwargs)

        fields = _auditable_fields(model, list(kwargs.keys()))
        if not fields:
            # Only auto_now touch columns changed — nothing substantive to log.
            return super().update(**kwargs)

        # Count first, before materializing anything: an update above the cap
        # collapses to one summary entry WITHOUT loading a PK (let alone a full
        # instance) per row — otherwise the guard would itself OOM on a
        # multi-million-row ngm table, the case it exists to make cheap.
        count = self.count()
        if count == 0:
            return super().update(**kwargs)
        if count > _max_rows():
            rows = super().update(**kwargs)
            _log_bulk_summary(model, count, fields)
            return rows

        # Snapshot pre-/post-image by PK from the WRITE db (never a replica, so
        # the post-read sees our own write); diff each row via the shared helper.
        write_db = self._db or router.db_for_write(model)
        base = model._base_manager.db_manager(write_db)
        pks = list(self.values_list("pk", flat=True))
        old = base.in_bulk(pks)
        rows = super().update(**kwargs)
        new = base.in_bulk(pks)
        for pk, old_obj in old.items():
            new_obj = new.get(pk)
            if new_obj is not None:
                log_bulk_update(old_obj, new_obj, fields=fields)
        return rows

    def bulk_update(self, objs, fields, batch_size=None):
        objs = list(objs)
        model = self.model
        if (
            _logging_disabled()
            or not objs
            or not fields
            or not auditlog.contains(model)
        ):
            return super().bulk_update(objs, fields, batch_size=batch_size)

        # ``fields`` drives the actual write; ``audit_fields`` (touch columns
        # stripped) drives the diff — keep them separate so the write is intact.
        audit_fields = _auditable_fields(model, list(fields))
        if not audit_fields:
            return super().bulk_update(objs, fields, batch_size=batch_size)

        write_db = self._db or router.db_for_write(model)
        base = model._base_manager.db_manager(write_db)
        pks = [obj.pk for obj in objs]

        if len(objs) > _max_rows():
            result = super().bulk_update(objs, fields, batch_size=batch_size)
            _log_bulk_summary(model, len(objs), audit_fields)
            return result

        old = base.in_bulk(pks)
        result = super().bulk_update(objs, fields, batch_size=batch_size)
        # ``objs`` already hold the new values (they are what was written).
        for obj in objs:
            old_obj = old.get(obj.pk)
            if old_obj is not None:
                log_bulk_update(old_obj, obj, fields=audit_fields)
        return result


def register_audited(model, **register_kwargs):
    """Register ``model`` with auditlog AND make its ``objects`` capture bulk writes.

    Call once per model from an ``AppConfig.ready()`` (in place of a bare
    ``auditlog.register(model)``). ``register_kwargs`` are forwarded verbatim to
    ``auditlog.register`` (``include_fields`` / ``exclude_fields`` / ``mask_fields``
    / ``m2m_fields``). At ``ready()`` time the model's default ``objects`` manager
    is swapped for one whose queryset mixes in :class:`AuditedQuerySet`, so
    ``Model.objects.filter(...).update(...)`` — the interactive pod-ORM /
    management-command / backfill pattern — is logged, not just ``save()``-driven
    writes.

    If the model already defines its own ``QuerySet`` override (e.g.
    ``cases.Case``'s slug-history ``update()``), :class:`AuditedQuerySet` is
    *woven in* rather than replacing it: the composed MRO runs the model's
    ``update()`` first, which calls ``super().update()`` into the audit hook,
    then the real DB write — so both behaviors run. Idempotent: a no-op if the
    queryset is already audited.
    """
    auditlog.register(model, **register_kwargs)

    existing = getattr(model, "objects", None)
    existing_qs = getattr(existing, "_queryset_class", models.QuerySet)
    if issubclass(existing_qs, AuditedQuerySet):
        return  # already audited
    if issubclass(AuditedQuerySet, existing_qs):
        # The model uses a plain QuerySet (a base of AuditedQuerySet), so the
        # audited queryset already subsumes it — use it directly.
        audited_qs = AuditedQuerySet
    else:
        # The model has its own QuerySet override; weave the audit hook in so
        # both ``update()`` overrides run via cooperative ``super()``.
        audited_qs = type(
            f"Audited{existing_qs.__name__}", (existing_qs, AuditedQuerySet), {}
        )

    # ``add_to_class`` alone would leave the pre-existing ``objects`` manager
    # shadowing the new one: ``Options.managers`` dedups by name and keeps the
    # first entry in ``local_managers``. Drop it first, then attach — that path
    # (``add_to_class`` -> ``Options.add_manager``) expires the manager caches,
    # so ``objects`` now resolves to the audited manager.
    opts = model._meta
    opts.local_managers = [m for m in opts.local_managers if m.name != "objects"]
    model.add_to_class("objects", audited_qs.as_manager())
