"""Material read-plane routes (mounted under /api/).

The material ``@id`` IRI is ``https://<base>/material/<source>/<ident>``; the
read endpoint mirrors its path component:

    GET /api/materials/?iri=<full-iri>            → JSON-LD by full IRI
    GET /api/materials/<source>/<ident>           → JSON-LD by IRI path component
    GET /api/materials/<source>/<ident>/content   → one kind of stored content
    GET /api/materials/<source>/<ident>/search    → hits inside that document

``<source>`` may be multi-segment (e.g. ``court``); the path converter keeps it
liberal. ``?iri=`` is matched first (bare ``/materials/`` list root).
"""

from django.urls import path, re_path

from . import views

# URL namespace — distinct from ngm-courts (both are mounted at /api/).
# Keeps reverse() / drf-spectacular operationIds unambiguous
# (``ngm-materials:material-detail``) without changing any URL PATH.
app_name = "ngm-materials"

urlpatterns = [
    # The source catalogue. Not under ``materials/`` because it is not a
    # material — it describes the shelves, and nesting it there would put it in
    # the path space the liberal ``<source>/<ident>`` pattern below claims.
    path("document-sources/", views.document_sources, name="document-sources"),
    path("materials/", views.material_by_iri, name="material-by-iri"),
    # File upload on the composite source/ident. Declared BEFORE material-detail
    # so the trailing ``/file`` subpath is not consumed by the liberal detail
    # ``ident`` pattern (``[^/]+`` stops at a slash, but declaring the specific
    # route first keeps the intent explicit and robust).
    re_path(
        r"^materials/(?P<source>[a-z0-9_]+(?:/[a-z0-9_]+){0,3})/(?P<ident>[^/]+)/file/?$",
        views.material_file_upload,
        name="material-file-upload",
    ),
    # Document content (pages, tables, figures). Declared BEFORE material-detail
    # for the same reason /file is, and it is not a style point: the detail
    # route's ``source`` is multi-segment, so it would otherwise match this URL
    # as source=``<source>/<ident>``, ident=``content`` and answer "no such
    # material" — a 404 that reads as a missing document rather than a missing
    # route. The cost is that ``content`` can never be a material ident.
    re_path(
        r"^materials/(?P<source>[a-z0-9_]+(?:/[a-z0-9_]+){0,3})/(?P<ident>[^/]+)/content/?$",
        views.material_content,
        name="material-content",
    ),
    # Search INSIDE one document — mixed page/table/figure hits with snippets.
    # Declared before material-detail for the same reason /content and /file
    # are, and it costs the same thing: ``search`` can never be a material
    # ident. The design note reserved both words together for this reason.
    re_path(
        r"^materials/(?P<source>[a-z0-9_]+(?:/[a-z0-9_]+){0,3})/(?P<ident>[^/]+)/search/?$",
        views.material_content_search,
        name="material-content-search",
    ),
    re_path(
        r"^materials/(?P<source>[a-z0-9_]+(?:/[a-z0-9_]+){0,3})/(?P<ident>[^/]+)/?$",
        views.material_detail,
        name="material-detail",
    ),
]
