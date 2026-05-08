"""
URL configuration for config project.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from cases.views import index, docs, legacy_case_redirect
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

urlpatterns = [
    path("", index, name="index"),
    # Legacy numeric redirects (priority match)
    path("case/<int:legacy_id>/", legacy_case_redirect, name="legacy-case-redirect"),
    path("case/<path:path>", index, name="case-detail-fallback"),
    path("entity/<path:path>", index, name="entity-detail-fallback"),
    path("docs/", docs, name="docs"),
    path("admin/", admin.site.urls),
    path("tinymce/", include("tinymce.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/swagger/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("api/", include("cases.urls")),
    path("api/", include("nesq.urls")),
    path("api/", include("ngm.urls")),
    # Case Workflows routes
    path("api/case-workflows/", include("case_workflows.urls")),
    # Caseworker Agent routes
    path("api/caseworker/", include("caseworker.urls")),
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
