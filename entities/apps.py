from django.apps import AppConfig


class EntitiesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "entities"
    label = "entities"

    def ready(self):
        # Audit the NES entity store. StoredEntity already keeps a rich JSON-LD
        # version history (StoredVersion + /versions); auditlog is additive —
        # it records who/when/field-diff and, via the audited manager, catches
        # pod-ORM ``update()`` writes that bypass the version-snapshotting path.
        # LogEntry rows are written cross-DB to ``default``; the long IRI PKs are
        # why cases migration 0051 widens ``object_pk`` to TEXT. StoredVersion
        # itself is skipped — it IS the entity audit trail.
        from jawafdehi_shared.db.audited import register_audited

        from entities.models import HeldEntity, StoredAuthor, StoredEntity

        # ``updated_at`` is excluded: it is set on every re-publish (it is a
        # ``default=timezone.now`` column, not ``auto_now``, so the manager's
        # touch-column filter does not catch it) and is not a substantive edit.
        register_audited(StoredEntity, exclude_fields=["updated_at"])
        register_audited(StoredAuthor)
        register_audited(HeldEntity)

        # Wire live unified-search indexing signals (best-effort, on_commit).
        from . import signals  # noqa: F401
