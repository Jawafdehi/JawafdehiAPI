"""Unified URL configuration — ONE ``/api/`` surface, no per-service prefixes.

All resources live under a single ``/api/`` root, keyed by resource kind (which
maps to the schema.org ``@id`` IRI shape), NOT by which former service owns them:

    /api/entities        -> entities.urls   (people, orgs, courts, firms, ...)
    /api/materials       -> materials.urls  (documents; file-bearing)
    /api/courtcases      -> courts.urls     (NGM court-case records + query + ingestion)
    /api/cases           -> cases.urls      (Jawafdehi CORRUPTION cases — DISTINCT)
    /api/search          -> search.urls
    /api/casework/       -> review.urls

HARD CUT (2026-07-01): the former ``/api/nes/`` and ``/api/ngm/`` prefixes are
REMOVED with NO redirects/aliases. Consumers are rewired in the same change.

Path collisions resolved by renaming (not prefixing): NGM court cases are
``courtcases`` (Jawafdehi owns ``cases``); the NGM case-party resolver is
``courtcase-entities`` (NES owns ``entities``); health is a single ``/api/health``.

URL-NAME NAMESPACES are retained (``nes`` / ``ngm-courts`` / ``ngm-materials``
``app_name``s) so ``reverse()`` and drf-spectacular operationIds stay unambiguous
even though the underlying route NAMES (``case`` / ``entity``) still collide. The
Jawafdehi ``cases.urls`` tree stays UN-namespaced (its bare names are what the
portal/casework clients reverse).
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic.base import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from wagtail.admin import urls as wagtailadmin_urls
from wagtail.documents import urls as wagtaildocs_urls

from cases.api_views import MeView, OEmbedView
from cases.views import docs, index
from content.api import api_router as wagtail_api_router

urlpatterns = [
    # Prometheus /metrics (django-prometheus). Not public — the ingress denies
    # external access to /metrics (metrics-deny-public middleware); scraped
    # in-cluster only.
    path("", include("django_prometheus.urls")),
    # ── Jawafdehi (default DB) — keeps the original /api/ paths ─────────────
    path("", index, name="index"),
    path("docs/", docs, name="docs"),
    # ── Public discovery surfaces (crawl + harvest) at the project ROOT ───────
    #   /sitemap.xml, /sitemap-<section>.xml, /robots.txt,
    #   /.well-known/resourcesync, /resourcesync/*.xml — the IRI-driven Sitemaps
    #   + ResourceSync that expose the public corpus by its canonical @id IRIs.
    path("", include("discovery.urls")),
    # Django's built-in admin lives at /django-admin/ so the SPA's React admin
    # panel can own the /admin/* path (served by the frontend, proxied per the
    # /api tree — never /admin). Superuser/model tasks stay reachable here.
    # Django-admin SSO: /django-admin/login/ -> Zitadel (mozilla-django-oidc),
    # bypassing the username/password form. Must precede admin.site.urls so the
    # explicit login route wins. /oidc/* is the authenticate + callback flow.
    path("oidc/", include("mozilla_django_oidc.urls")),
    path(
        "django-admin/login/",
        RedirectView.as_view(url="/oidc/authenticate/", query_string=True),
        name="admin-oidc-login",
    ),
    path("django-admin/", admin.site.urls),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/swagger/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    # ── Unified platform search. Mounted BEFORE cases.urls so it owns /api/search/. ──
    path("api/search/", include("search.urls")),
    # ── One /api/ surface. NES entities + NGM courts/materials are mounted at the
    #    SAME /api/ root as Jawafdehi; collisions were renamed away (courtcases,
    #    courtcase-entities) so each include owns distinct paths. The prefixed
    #    includes come BEFORE cases.urls (whose router has a catch-all-ish tree). ──
    path("api/", include("entities.urls")),
    path("api/", include("courts.urls")),
    path("api/", include("materials.urls")),
    path("api/", include("newsletter.urls")),
    # Case-update proposals — before cases.urls (whose router is broad).
    path("api/", include("case_proposals.urls")),
    path("api/", include("case_tags.urls")),
    # Filing a signal by hand — the one producer a human drives. Also before
    # cases.urls, for the same reason.
    path("api/", include("case_events.urls")),
    path("api/", include("cases.urls")),
    path("oembed/", OEmbedView.as_view(), name="oembed"),
    path("api/caseworker/me", MeView.as_view(), name="cw-me"),
    path("api/casework/", include("review.urls")),
    # ── Central job queue (platform-wide; consumers claim/stage/result here) ──
    path("api/jobs/", include("jobs.urls")),
    # ── Wagtail CMS ("Jawafdehi Newsroom"): editorial admin, document serving,
    #    and the headless API v2 the SPA's /updates section consumes. The API
    #    contract is FROZEN — the frontend calls /api/cms/v2/pages/ (with
    #    ?type=content.ArticlePage, fields, order) and /api/cms/v2/page_preview/.
    #    Retire Wagtail's built-in password form: send /newsroom/login/ to the
    #    same Zitadel OIDC SSO the rest of the platform uses. Must precede the
    #    wagtailadmin include so it wins URL resolution.
    path(
        "newsroom/login/",
        RedirectView.as_view(
            url="/oidc/authenticate/?next=/newsroom/", query_string=False
        ),
    ),
    path("newsroom/", include(wagtailadmin_urls)),
    path("documents/", include(wagtaildocs_urls)),
    path("api/cms/v2/", wagtail_api_router.urls),
]

# Local-dev auth (DEV_AUTH, DEBUG/TESTING-only): DRF session login/logout at
# /api-auth/ so a developer can authenticate with a plain username/password
# instead of standing up Zitadel. Never mounted in production.
if settings.DEV_AUTH:
    urlpatterns += [
        path("api-auth/", include("rest_framework.urls", namespace="rest_framework")),
    ]

if settings.DEBUG and str(settings.MEDIA_URL).startswith("/"):
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
