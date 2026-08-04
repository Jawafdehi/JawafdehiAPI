"""Database router for the unified Django project.

The three formerly-separate services (NES / NGM / Jawafdehi) now live as Django
*apps* in ONE project, but they KEEP their three separate Postgres databases
(the database-per-service decision survives the collapse). This router is what
makes that work: it pins each app's models to its own database connection so
that there is never a cross-database query or FK.

    app_label              ->  database alias
    ------------------------------------------
    entities               ->  "nes"        (entities)
    courts                 ->  "ngm"        (courts)
    materials              ->  "ngm"        (materials)
    <everything else>      ->  "default"    (jawafdehi: cases / review +
                                             django.contrib.*)

Cross-app access stays in-process but is NEVER a DB join: e.g. the
``cases.services.nes_resolver`` seam queries ``entities`` models,
which this router routes to the ``nes`` DB, then joins the results in Python
against ``cases`` rows (which live in ``default``). No ``ForeignKey`` ever
crosses a database, so ``allow_relation`` only permits relations *within* the
same database, and ``allow_migrate`` keeps each app's tables in its own DB.

App labels (not the dotted ``name``) are used for routing because that is what
``model._meta.app_label`` exposes. The NES/NGM apps use the short labels
``entities`` / ``courts`` / ``materials`` (``materials`` is pinned explicitly in
its AppConfig; ``courts``/``entities`` are the default label derived from the
dotted ``name``).
"""

from __future__ import annotations

import contextvars

# Read-routing flag, set by config.middleware.ReadReplicaRoutingMiddleware for the
# duration of a request: True → this request's reads may use the read replica;
# False (default, and outside any request — mgmt commands, shell, tasks) → reads
# use the primary. A ContextVar (not threading.local) so it is correct under BOTH
# sync gunicorn worker threads AND async views/tasks: each thread has its own
# top-level context and each async task copies it, so the flag never leaks across
# concurrent requests. Kept here (next to the router that consumes it) so the
# router has no import-time dependency on the middleware.
_use_replica_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "use_replica_for_reads", default=False
)


def route_reads_to_replica(enabled: bool) -> None:
    """Mark the current context's reads as replica-eligible (True) or primary-only
    (False). The middleware sets it per request and always resets it afterwards."""
    _use_replica_var.set(enabled)


def _reads_use_replica() -> bool:
    return _use_replica_var.get()


# App labels owned by each service database. Anything not listed routes to
# "default" (the Jawafdehi database, which also owns django.contrib.* — auth,
# admin, sessions, contenttypes — since that is the only app that uses Users).
NES_APP_LABELS = frozenset({"entities"})
NGM_APP_LABELS = frozenset({"courts", "materials"})

# django.contrib apps that must live wherever the User/auth tables live. The
# Jawafdehi app is the only one with FKs/M2Ms to auth.User, so contrib lives in
# "default" with Jawafdehi. (Listing them is documentation; they already fall
# through to "default" by default.)
_DEFAULT_CONTRIB = frozenset(
    {"admin", "auth", "contenttypes", "sessions", "messages", "auditlog"}
)

# The Wagtail CMS ("content" app + Wagtail core/images/documents/etc. and its
# deps taggit/modelcluster) also lives on "default": its page tree FKs to the
# same auth.User and it cross-links cases.Case (ArticlePage.related_cases_through), which
# is on "default" too. These fall through to "default" by _db_for_label already
# (they're in neither NES nor NGM); listing them is documentation only, mirroring
# _DEFAULT_CONTRIB. Do NOT route any of these to the nes/ngm DBs.
_DEFAULT_WAGTAIL = frozenset(
    {
        "content",
        "wagtailcore",
        "wagtailadmin",
        "wagtaildocs",
        "wagtailimages",
        "wagtailembeds",
        "wagtailsearch",
        "wagtailredirects",
        "wagtailforms",
        "wagtailusers",
        "wagtailsnippets",
        "taggit",
    }
)


def _db_for_label(app_label: str) -> str:
    if app_label in NES_APP_LABELS:
        return "nes"
    if app_label in NGM_APP_LABELS:
        return "ngm"
    return "default"


class ServiceDatabaseRouter:
    """Route each service-app's models to that service's database."""

    def db_for_read(self, model, **hints):
        primary = _db_for_label(model._meta.app_label)
        # Anonymous public reads (flagged by the middleware) may use the read
        # replica if one is configured for this service; everything else — writes,
        # admin/casework, mgmt commands (no flag) — reads from the primary so it
        # always sees its own writes. Import settings lazily to avoid an
        # import-time cycle (settings imports this module as DATABASE_ROUTERS).
        if _reads_use_replica():
            from django.conf import settings

            return settings.REPLICA_ALIASES.get(primary, primary)
        return primary

    def db_for_write(self, model, **hints):
        return _db_for_label(model._meta.app_label)

    def allow_relation(self, obj1, obj2, **hints):
        """Only allow ORM relations between objects in the SAME database.

        There are no cross-service FKs by design (cross-app links are stored as
        opaque string keys — the NES @id IRI, the ``court:case_number`` tuple —
        and resolved in Python). Returning ``False`` for cross-DB pairs makes any
        accidental cross-database relation fail loudly rather than silently
        issue a broken query. Returning ``None`` (no opinion) for same-DB pairs
        lets Django apply its default (allow).
        """
        db1 = _db_for_label(obj1._meta.app_label)
        db2 = _db_for_label(obj2._meta.app_label)
        return db1 == db2

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """Each app's tables are created ONLY in that app's database.

        ``migrate --database=nes`` applies only the ``entities`` app (+ nothing
        else); ``--database=ngm`` only ``courts``/``materials``; ``--database=
        default`` everything else (Jawafdehi apps + django.contrib.*). This keeps
        each physical DB holding exactly its own app's tables — no stray
        ``cases`` table in the ``nes`` DB, etc.
        """
        target = _db_for_label(app_label)
        return db == target
