from django.contrib.contenttypes.models import ContentType
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import NotFound, ValidationError
from wagtail.api.v2.router import WagtailAPIRouter
from wagtail.api.v2.views import PagesAPIViewSet
from wagtail.documents.api.v2.views import DocumentsAPIViewSet
from wagtail.images.api.v2.views import ImagesAPIViewSet
from wagtail.models import Locale
from wagtail_headless_preview.models import PagePreview


class PagePreviewAPIViewSet(PagesAPIViewSet):
    """Serialize an unsaved draft for the headless preview iframe.

    ``HeadlessPreviewMixin.serve_preview`` stores the in-progress edit in a
    ``PagePreview`` row keyed by a signed token and redirects the iframe to the
    SPA with ``?content_type=&token=``. The SPA calls the *detail* route with
    those params (the pk in the path is a placeholder — ``get_object`` resolves
    the draft from the token instead). Resolving the draft is all we override:
    the inherited ``detail_view`` serializes it through the same v2 machinery as
    a published page — specific page type, full detail fields — so
    ``ArticlePage.api_fields`` (StreamField body, related cases, …) render
    identically in preview.
    """

    def get_object(self):
        # The draft lives in a signed-token cache row, not the page queryset, so
        # resolve it directly. Cached because the base detail_view and
        # get_serializer_class both call this within a single request.
        if not hasattr(self, "_cached_object"):
            try:
                app_label, model = self.request.GET["content_type"].split(".")
                token = self.request.GET["token"]
            except (KeyError, ValueError):
                raise ValidationError(
                    "Both 'content_type' (as 'app_label.model') and 'token' "
                    "are required."
                )
            content_type = get_object_or_404(
                ContentType, app_label=app_label, model=model
            )
            page_preview = get_object_or_404(
                PagePreview, content_type=content_type, token=token
            )
            page = page_preview.as_page()
            # A never-saved draft (previewed before its first save) has neither a
            # pk nor a locale — the serializer's detail_url and non-nullable
            # locale field need both — so fall back to sensible values. The page
            # object is only serialized, never persisted.
            if page.pk is None:
                page.pk = 0
            if page.locale_id is None:
                page.locale = Locale.get_default()
            self._cached_object = page
        return self._cached_object

    def listing_view(self, request):
        # Previews are fetched by token via the detail route; don't expose a
        # second (published-only) page listing under this endpoint.
        raise NotFound()


# Headless delivery for the public SPA. The `pages` endpoint returns only
# published/live pages; `page_preview` returns the current draft by signed
# token for the editor preview iframe.
api_router = WagtailAPIRouter("wagtailapi")
api_router.register_endpoint("pages", PagesAPIViewSet)
api_router.register_endpoint("images", ImagesAPIViewSet)
api_router.register_endpoint("documents", DocumentsAPIViewSet)
api_router.register_endpoint("page_preview", PagePreviewAPIViewSet)
