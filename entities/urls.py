"""NES API routes — JSON-LD entity read/write plane + admin.

The entity detail ``{ref}`` is either a url-encoded ``@id`` IRI
(``https%3A%2F%2Fjawafdehi.org%2Fentity%2Fperson%2Fram-bahadur``) or the bare
``<prefix>/<slug>`` path (``person/ram-bahadur``) — both contain slashes, so the
id-bearing routes use ``re_path`` with a permissive capture rather than DRF's pk
router. The ``/versions`` suffix route is declared BEFORE the bare detail route;
the ref pattern is non-greedy so the trailing literal segment wins.
"""
from django.urls import path, re_path

from . import views

# URL namespace. The three former services' URLConfs are mounted in one project
# (config.urls) and several route NAMES/DRF basenames collide across
# them (NES, NGM-courts and Jawafdehi all define e.g. ``entity``/``case``/
# ``search``). Namespacing this conf keeps reverse() / drf-spectacular
# operationIds unambiguous (``nes:entity-detail`` etc.) without changing any
# URL PATH.
app_name = "nes"

# A url-encoded IRI or a prefix/slug path — any run of non-query chars. Non-greedy
# so the trailing ``/versions`` suffix route matches its literal segment instead
# of being swallowed into the ref.
_REF = r"(?P<ref>[^?]+?)"

urlpatterns = [
    path("health", views.health),
    path("entities/tags", views.list_tags, name="entity-tags"),
    path("entity_prefixes", views.list_entity_prefixes, name="entity-prefixes"),
    path("admin/reindex", views.ReindexView.as_view(), name="admin-reindex"),
    # List + create.
    path("entities", views.EntityListCreateView.as_view(), name="entity-list"),
    # Merge (must precede the bare detail route, which captures any {ref}).
    re_path(r"^entities/merge/?$", views.EntityMergeView.as_view(), name="entity-merge"),
    # Entity sub-resources (must precede the bare detail route).
    re_path(
        rf"^entities/{_REF}/versions/?$",
        views.EntityVersionsView.as_view(),
        name="entity-versions",
    ),
    # Entity detail (GET) + patch (PATCH).
    re_path(
        rf"^entities/{_REF}/?$",
        views.EntityDetailView.as_view(),
        name="entity-detail",
    ),
]
