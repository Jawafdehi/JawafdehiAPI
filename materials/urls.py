"""Material read-plane routes (mounted under /api/).

The material ``@id`` IRI is ``https://<base>/material/<source>/<ident>``; the
read endpoint mirrors its path component:

    GET /api/materials/?iri=<full-iri>     → JSON-LD by full IRI
    GET /api/materials/<source>/<ident>    → JSON-LD by IRI path component

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
    re_path(
        r"^materials/(?P<source>[a-z0-9_]+(?:/[a-z0-9_]+){0,3})/(?P<ident>[^/]+)/?$",
        views.material_detail,
        name="material-detail",
    ),
]
