"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
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
    path("", index, name="index"),
    path("docs/", docs, name="docs"),
    path("admin/", admin.site.urls),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/swagger/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("api/", include("cases.urls")),
    path("oembed/", OEmbedView.as_view(), name="oembed"),
    path("api/", include("nesq.urls")),
    path("api/", include("ngm.urls")),
    # Case Workflows routes
    path("api/case-workflows/", include("case_workflows.urls")),
    # Chat identity resolution for the jawafdehi-mcp server. This is the lone
    # surviving endpoint from the removed caseworker agent app; the path is kept
    # for backward compatibility with the MCP server.
    path("api/caseworker/me", MeView.as_view(), name="cw-me"),
    # Casework Review System (VOL-3) — rule-centered case-quality review.
    # Auth: OIDC JWT + Contributor role.
    path("api/casework/", include("review.urls")),
    # OIDC authentication for admin SSO
    path("oidc/", include("mozilla_django_oidc.urls")),
    # Wagtail CMS: editorial admin, document serving, and headless API v2.
    # Retire Wagtail's built-in password form — send /newsroom/login/ to OIDC SSO.
    # Must precede the wagtailadmin include so it wins URL resolution.
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

if settings.DEBUG and str(settings.MEDIA_URL).startswith("/"):
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
