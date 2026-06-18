"""Single source of truth for managed group -> permission policy.

Group *membership* is Zitadel-authoritative (see config/roles.py:
``sync_user_roles`` mirrors OIDC roles -> Django groups on every login). This
module defines what each group may *do* and applies it authoritatively and
idempotently:

- via ``content``'s ``post_migrate`` handler (auto, every ``migrate``), and
- via ``manage.py create_groups`` (manual entry point / backfill),

so there is never a separate sync step. ``admin`` maps to ``is_superuser`` and
bypasses all of this; the rows below only matter for non-superuser tiers.

Permissions are reconciled to *exactly* the declared sets (authoritative): a
hand-added permission on a managed group is removed on the next run.
"""

# Django models used for the case-domain permissions. (DocumentSourceUpload was
# removed in cases migration 0033; its stale prod permission rows, if any, are
# reconciled away by the authoritative sync below.)
_CASE_MODELS = (
    "case",
    "caseentityrelationship",
    "documentsource",
    "jawafentity",
)


def _cases(ops_by_model):
    """Expand {model: (ops,)} into ['cases.<op>_<model>', ...]."""
    return [f"cases.{op}_{model}" for model, ops in ops_by_model.items() for op in ops]


_FULL = {m: ("add", "change", "delete", "view") for m in _CASE_MODELS}
_VIEW_ONLY = {m: ("view",) for m in _CASE_MODELS}
_CONTRIBUTOR = {
    "case": ("add", "change", "view"),
    "caseentityrelationship": ("add", "change", "delete", "view"),
    "documentsource": ("add", "change", "view"),
    "jawafentity": ("add", "view"),
}
_REVIEW_ASSISTANT = {
    "documentsource": ("change", "view"),
}

_ACCESS_ADMIN = ["wagtailadmin.access_admin"]
_ALL_PAGE_PERMS = [
    "add_page",
    "change_page",
    "publish_page",
    "bulk_delete_page",
    "lock_page",
    "unlock_page",
]
_EDITOR_PAGE_PERMS = ["add_page", "change_page"]


def _collection(ops):
    return [f"{op}_{model}" for model in ("image", "document") for op in ops]


_FULL_COLLECTION = _collection(("add", "change", "delete", "choose"))
_EDITOR_COLLECTION = _collection(("add", "change", "choose"))

# group -> django perms ("<app_label>.<codename>", incl. wagtailadmin.access_admin)
GROUP_DJANGO_PERMS = {
    "Admin": _cases(_FULL) + _ACCESS_ADMIN,
    "Moderator": _cases(_FULL) + _ACCESS_ADMIN,
    "Contributor": _cases(_CONTRIBUTOR) + _ACCESS_ADMIN,
    "ReadOnly": _cases(_VIEW_ONLY),
    "ReviewAssistant": _cases(_REVIEW_ASSISTANT),
}

# group -> Wagtail page-permission codenames on the Articles index (absent = none)
GROUP_PAGE_PERMS = {
    "Admin": _ALL_PAGE_PERMS,
    "Moderator": _ALL_PAGE_PERMS,
    "Contributor": _EDITOR_PAGE_PERMS,
}

# group -> Wagtail collection-permission codenames on the root collection
GROUP_COLLECTION_PERMS = {
    "Admin": _FULL_COLLECTION,
    "Moderator": _FULL_COLLECTION,
    "Contributor": _EDITOR_COLLECTION,
}

MANAGED_GROUPS = tuple(GROUP_DJANGO_PERMS)


def _perm(dotted):
    from django.contrib.auth.models import Permission

    app_label, codename = dotted.split(".", 1)
    return Permission.objects.filter(
        content_type__app_label=app_label, codename=codename
    ).first()


def _collection_perm(codename):
    from django.contrib.auth.models import Permission

    is_image = codename.endswith("_image")
    return Permission.objects.filter(
        content_type__app_label="wagtailimages" if is_image else "wagtaildocs",
        content_type__model="image" if is_image else "document",
        codename=codename,
    ).first()


def _reconcile_collection_scoped(model, group, scope_kwargs, want_codenames, resolve):
    """Reconcile a (group, scope) set of permission rows to exactly want."""
    existing = {
        row.permission.codename: row
        for row in model.objects.filter(group=group, **scope_kwargs).select_related(
            "permission"
        )
    }
    for codename in want_codenames:
        if codename not in existing:
            perm = resolve(codename)
            if perm:
                model.objects.create(group=group, permission=perm, **scope_kwargs)
    for codename, row in existing.items():
        if codename not in want_codenames:
            row.delete()


def sync_groups():
    """Create/reconcile every managed group to its declared permission set.

    Authoritative + idempotent. Returns the number of groups processed. Safe to
    call repeatedly; expects the Wagtail/content tables to exist (callers guard).
    """
    from django.contrib.auth.models import Group
    from wagtail.models import (
        Collection,
        GroupCollectionPermission,
        GroupPagePermission,
    )

    from content.permissions import ensure_article_index

    index = ensure_article_index()
    root_collection = Collection.objects.filter(depth=1).order_by("path").first()

    for name, dotted_perms in GROUP_DJANGO_PERMS.items():
        group, _ = Group.objects.get_or_create(name=name)

        # Django model permissions (authoritative).
        group.permissions.set([p for p in (_perm(d) for d in dotted_perms) if p])

        # Wagtail page permissions on the Articles index.
        if index is not None:
            _reconcile_collection_scoped(
                GroupPagePermission,
                group,
                {"page": index},
                set(GROUP_PAGE_PERMS.get(name, [])),
                lambda c: _perm(f"wagtailcore.{c}"),
            )

        # Wagtail collection permissions on the root collection.
        if root_collection is not None:
            _reconcile_collection_scoped(
                GroupCollectionPermission,
                group,
                {"collection": root_collection},
                set(GROUP_COLLECTION_PERMS.get(name, [])),
                _collection_perm,
            )

    return len(GROUP_DJANGO_PERMS)
