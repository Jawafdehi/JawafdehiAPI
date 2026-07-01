"""NGM API routes (read plane + gated query + ingestion).

Mounted at the unified ``/api/`` root (no ``/ngm`` prefix). Court cases are
exposed as ``courtcases`` (NOT ``cases`` — that path belongs to Jawafdehi
corruption cases) mirroring their canonical @id ``/courtcase/<court>/<num>``.
The case-party resolver is ``courtcase-entities`` (NES entities own ``entities``).

The case sub-resources are keyed on the composite (court, case_number), so those
paths are declared explicitly *before* the router's ``courtcases`` registration —
the router only handles the bare ``/courtcases`` list. Path converters are kept
liberal (``[^/]+``) because case numbers contain hyphens.
"""
from django.urls import path, re_path
from rest_framework.routers import DefaultRouter

from . import views

# URL namespace. Mounted alongside NES + Jawafdehi at the one ``/api/`` root;
# route names / DRF basenames such as ``case`` and ``entity`` collide with those
# trees. Namespacing keeps reverse() / drf-spectacular operationIds unambiguous
# (``ngm-courts:case-detail`` etc.).
app_name = "ngm-courts"

router = DefaultRouter()
router.register("courts", views.CourtViewSet, basename="court")
router.register("courtcases", views.CourtCaseViewSet, basename="case")
router.register("courtcase-entities", views.CaseEntityViewSet, basename="entity")
router.register("firms", views.BlacklistedFirmViewSet, basename="firm")

# Composite-key case detail + sub-resources. {court} and {case_number} are any
# non-slash run (case numbers contain hyphens, e.g. 082-OA-0503).
_case = r"courtcases/(?P<court>[^/]+)/(?P<case_number>[^/]+)"

composite_case_urls = [
    re_path(
        rf"^{_case}/hearings/?$",
        views.CourtCaseViewSet.as_view({"get": "list_hearings"}),
        name="case-hearings",
    ),
    re_path(
        rf"^{_case}/entities/?$",
        views.CourtCaseViewSet.as_view({"get": "list_entities"}),
        name="case-entities",
    ),
    re_path(
        rf"^{_case}/documents/?$",
        views.CourtCaseViewSet.as_view({"get": "list_documents"}),
        name="case-documents",
    ),
    re_path(
        rf"^{_case}/?$",
        views.CourtCaseViewSet.as_view(
            {
                "get": "retrieve_composite",
                "put": "update_composite",
                "patch": "update_composite",
            }
        ),
        name="case-detail",
    ),
]

# Hand-written routes use a trailing slash to match the DRF router convention
# (the router registers /courts/, /cases/, etc. with trailing slashes). The
# composite-case re_paths above keep the slash optional (``/?$``) because their
# tails (.../hearings, .../entities) are sub-resource segments, not list roots.
urlpatterns = [
    # NGM health is served by the single canonical /api/health (entities.urls);
    # the per-plane duplicate was dropped in the unified-surface cutover.
    path("query/", views.QueryView.as_view(), name="query"),
    # NOTE: the NGM 501 search stub was removed in the unified-search cutover.
    # Platform search lives at ``GET /api/search/`` (the ``search`` app), which
    # indexes NGM materials + court cases alongside entities and cases.
    path("ingestion/cases/", views.IngestionCasesView.as_view(), name="ingestion-cases"),
    path(
        "ingestion/entities/resolve/",
        views.IngestionEntitiesResolveView.as_view(),
        name="ingestion-entities-resolve",
    ),
    path(
        "ingestion/documents/",
        views.IngestionDocumentsView.as_view(),
        name="ingestion-documents",
    ),
    *composite_case_urls,
    *router.urls,
]
