"""Unified URL configuration for the consolidated platform (monolith).

The three former services are mounted under DISTINCT path prefixes so their
route trees don't collide. Each former app's ``urls.py`` assumed it was mounted
at ``/api/`` in its own project; several of those trees overlap (NGM and
Jawafdehi BOTH define ``cases/``, ``search/``, and ``entities/`` — for entirely
different models). Mounting them at the same prefix would shadow one another, so
each service keeps its own routes under its own prefix:

    /api/nes/   -> nes_service.entities.urls   (entities/, entity_prefixes, ...)
    /api/ngm/   -> ngm_service.courts.urls + ngm_service.materials.urls
                   (courts/, cases/, query/, search/, materials/, ...)
    /api/       -> Jawafdehi (cases/, sources/, search/, statistics/, ...)
    /api/casework/        -> review.urls

All platform APIs now live under a single ``/api/`` root: Jawafdehi at the bare
``/api/`` tree (unchanged paths), NES under ``/api/nes/`` and NGM under
``/api/ngm/``. The NES/NGM subtrees are mounted BEFORE the bare ``/api/`` include
so they own their prefixes unambiguously. (Earlier these lived at ``/nes/api/`` /
``/ngm/api/``; renamespaced to ``/api/nes/`` / ``/api/ngm/`` 2026-06-30.)

URL-NAME NAMESPACES: beyond the path prefixes, several route NAMES and DRF
router basenames also collide across the trees (Jawafdehi, NES and NGM-courts
all define ``case`` / ``search`` / ``entity``). To keep ``reverse()`` and
drf-spectacular operationIds unambiguous, the NES + NGM URLConfs declare an
``app_name`` (``nes`` / ``ngm-courts`` / ``ngm-materials``), so their names are
reached as ``nes:entity-detail``, ``ngm-courts:case-detail``, etc. The
Jawafdehi ``cases.urls`` tree is intentionally left UN-namespaced — it is the
primary ``/api/`` tree and its bare names (``case-list``, ``unified-search``,
...) are what the portal/casework clients already reverse. The URL *paths* are
unchanged by namespacing.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from cases.api_views import MeView, OEmbedView
from cases.views import docs, index

urlpatterns = [
    # ── Jawafdehi (default DB) — keeps the original /api/ paths ─────────────
    path("", index, name="index"),
    path("docs/", docs, name="docs"),
    # ── Public discovery surfaces (crawl + harvest) at the project ROOT ───────
    #   /sitemap.xml, /sitemap-<section>.xml, /robots.txt,
    #   /.well-known/resourcesync, /resourcesync/*.xml — the IRI-driven Sitemaps
    #   + ResourceSync that expose the public corpus by its canonical @id IRIs.
    path("", include("monolith.discovery.urls")),
    # Django's built-in admin lives at /django-admin/ so the SPA's React admin
    # panel can own the /admin/* path (served by the frontend, proxied per the
    # /api tree — never /admin). Superuser/model tasks stay reachable here.
    path("django-admin/", admin.site.urls),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/swagger/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    # ── Unified platform search (replaces the old cases-scoped /api/search/ and
    #    the NGM 501 stub). Mounted BEFORE cases.urls so it owns /api/search/. ──
    path("api/search/", include("monolith.search.urls")),
    # ── NES (nes DB) — mounted BEFORE the bare /api/ (cases) tree so the
    #    /api/nes/ subtree owns its prefix unambiguously. ──────────────────────
    path("api/nes/", include("nes_service.entities.urls")),
    # ── NGM (ngm DB) ─────────────────────────────────────────────────────────
    path("api/ngm/", include("ngm_service.courts.urls")),
    path("api/ngm/", include("ngm_service.materials.urls")),
    path("api/", include("cases.urls")),
    path("oembed/", OEmbedView.as_view(), name="oembed"),
    path("api/caseworker/me", MeView.as_view(), name="cw-me"),
    path("api/casework/", include("review.urls")),
]

if settings.DEBUG and str(settings.MEDIA_URL).startswith("/"):
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
