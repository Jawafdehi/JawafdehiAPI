"""DRF ↔ django-auditlog glue.

Closes two gaps in the platform's audit trail:

1. **Actor is NULL on every API-driven audit entry.** ``auditlog.middleware.AuditlogMiddleware``
   captures ``request.user`` at the Django-middleware layer, which runs *before* DRF
   authentication resolves the user in the view (the platform authenticates with
   ``jawafdehi_shared.auth.oidc.OIDCAuthentication``). For token/OIDC clients the middleware
   therefore only ever sees ``AnonymousUser``, so every ``LogEntry`` is written with
   ``actor=NULL``. :class:`AuditlogActorMixin` re-binds the actor once DRF has authenticated,
   reusing the request context the middleware already established so the ``remote_addr`` /
   ``remote_port`` / ``cid`` it captured are preserved.

2. **Writes made with ``QuerySet.update()`` / bulk ops are never logged.** auditlog hooks
   ``post_save`` / ``post_delete``; a bulk ``update()`` emits neither, so those edits vanish
   from the trail (this is why content PATCHes on cases were invisible while workflow saves
   were not). :func:`log_bulk_update` writes the equivalent UPDATE entry explicitly — call it
   right after the bulk write.

The mixin lives here (not in a single app) so any DRF write endpoint touching an
auditlog-registered model can opt in by inheritance.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model

from auditlog import get_logentry_model
from auditlog.context import auditlog_value
from auditlog.diff import model_instance_diff


def _authenticated_actor(request):
    """Return the request's authenticated user model instance, or ``None``."""
    user = getattr(request, "user", None)
    if isinstance(user, get_user_model()) and user.is_authenticated:
        return user
    return None


def bind_actor_to_auditlog(request) -> None:
    """Inject the DRF-authenticated user into auditlog's ambient request context.

    ``AuditlogMiddleware`` establishes the context (with ``actor=None`` for API clients,
    since DRF auth hasn't run yet) and connects the ``pre_save`` receiver that reads it when
    a ``LogEntry`` is saved. We mutate that same context dict in place, so the actor is
    resolved at *save* time and ``remote_addr`` / ``remote_port`` / ``cid`` survive.

    Safe no-op when there is no authenticated user (anonymous public reads), when no auditlog
    context is active (e.g. the middleware isn't installed, as in some unit tests), or when an
    actor is already bound (don't clobber a value the middleware managed to capture, e.g. a
    session-authenticated admin request).
    """
    actor = _authenticated_actor(request)
    if actor is None:
        return
    try:
        context = auditlog_value.get()
    except LookupError:
        return
    if context.get("actor") is None:
        context["actor"] = actor


class AuditlogActorMixin:
    """DRF view mixin that attributes audit entries to the authenticated user.

    Mix into any ``APIView`` / ``ViewSet`` whose writes touch auditlog-registered models.
    ``initial()`` runs after DRF authentication, so ``request.user`` is the real OIDC/token
    user rather than the middleware's ``AnonymousUser``.
    """

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        bind_actor_to_auditlog(request)


def log_bulk_update(old_instance, new_instance, *, fields=None, force_log=False):
    """Write an auditlog UPDATE entry for a change that bypassed ``post_save``.

    auditlog only logs saves that emit ``post_save``; ``QuerySet.update()`` and other bulk
    writes don't, so those edits leave no trail. Capture a clean instance *before* the write
    (``old_instance``) and *after* (``new_instance``) and call this to record the diff, in the
    same format and with the same actor attribution as a signal-generated entry (the ambient
    context — see :class:`AuditlogActorMixin` / ``AuditlogMiddleware`` — supplies actor and
    ``remote_addr`` via the ``pre_save`` receiver on ``LogEntry``).

    :param fields: restrict the diff to these field names (typically the ``update()`` keys),
        mirroring how the signal path passes ``save(update_fields=...)``.
    :returns: the created ``LogEntry``, or ``None`` when nothing changed and ``force_log`` is
        false.
    """
    changes = model_instance_diff(
        old_instance,
        new_instance,
        fields_to_check=fields,
        use_json_for_changes=settings.AUDITLOG_STORE_JSON_CHANGES,
    )
    if not (changes or force_log):
        return None
    log_entry_model = get_logentry_model()
    return log_entry_model.objects.log_create(
        new_instance,
        action=log_entry_model.Action.UPDATE,
        changes=changes,
        force_log=force_log,
    )
