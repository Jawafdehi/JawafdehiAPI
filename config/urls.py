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
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from cases.api_views import MeView, OEmbedView
from cases.views import docs, index

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
    # Auth: shared JWT (token from /api/caseworker/auth/token/) + Contributor role.
    path("api/casework/", include("review.urls")),
    path(
        "api/caseworker/auth/token/",
        TokenObtainPairView.as_view(),
        name="cw-token-obtain",
    ),
    path(
        "api/caseworker/auth/token/refresh/",
        TokenRefreshView.as_view(),
        name="cw-token-refresh",
    ),
]

if settings.DEBUG and str(settings.MEDIA_URL).startswith("/"):
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
