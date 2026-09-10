"""Case image upload.

One endpoint, ``POST /api/case-images/``: take a multipart image from the case
editor, put it in the Wagtail image library, and hand back the id plus both
rendition ladders so the editor can preview the result immediately. The case
itself is then pointed at that id by the ordinary RFC-6902 PATCH
(``replace /thumbnail_image_id``), so there is still exactly one write path onto
a Case.

Validation is Wagtail's own ``WagtailImageField``, not a hand-rolled allowlist.
It already enforces the extension allowlist, that the *content* matches the
extension (a PSD renamed ``.jpg`` is rejected), ``WAGTAILIMAGES_MAX_UPLOAD_SIZE``,
and ``WAGTAILIMAGES_MAX_IMAGE_PIXELS`` — the decompression-bomb guard, which
matters here because the uploads are attacker-influenceable in a way the CMS's
staff-only image chooser is not. It also leaves a Willow image on the cleaned
file, which is where ``width``/``height`` come from: those columns are
``editable=False`` and nothing in ``Image.save()`` populates them, so an image
created in code without them raises ``IntegrityError``.
"""

from __future__ import annotations

import os
import uuid

from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from wagtail.images import get_image_model
from wagtail.images.fields import WagtailImageField
from wagtail.utils.file import hash_filelike

from .image_serializers import (
    CARD_SPECS,
    HERO_SPECS,
    SRCSET_SCHEMA,
    SrcsetRenditionField,
)
from .permissions import IsCaseImageUploader

#: The upload response body. Shared by 201 (created) and 200 (these bytes were
#: already in the library) — same shape either way.
_UPLOAD_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "title": {"type": "string"},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
        "thumbnail": SRCSET_SCHEMA,
        "banner": SRCSET_SCHEMA,
    },
}


def unique_upload_name(uploaded_file) -> str:
    """Rename an upload to ``<uuid4><ext>``, unique per upload.

    This is a correctness fix, not a tidiness one. ``default_storage`` is
    ``HashedFilenameS3Boto3Storage``, which derives the object key from a salted
    hash of the *filename* and — deliberately, see its ``get_available_name`` —
    never de-duplicates. Wagtail hands it ``original_images/<filename>``, so two
    different images uploaded as ``photo.jpg`` produce one key and the second
    silently overwrites the first. That behaviour is right for the document
    ingestion path it was built for, where the name is a stable identifier; it is
    data loss for images a person picked off their desktop, where ``photo.jpg``
    and ``IMG_0001.jpg`` are the common case.

    The name is random rather than a hash of the CONTENT, which is what this
    started as. Content-addressing fixed the collision above but introduced a
    second one: identical bytes get an identical name, so two ``Image`` rows for
    the same picture share one object — and Wagtail deletes an image's file when
    its row is deleted (``post_delete_file_cleanup``), so removing either row
    blanks the other's cases, renditions included. The ``file_hash`` lookup in
    :meth:`CaseImageUploadView.post` avoids the second row on the common path,
    but it is a check-then-act: two concurrent uploads of the same file (a
    double-click on Replace is enough) can both pass it. A unique name means
    that race costs one redundant object instead of a latent shared one, so
    safety no longer depends on winning it. De-duplication is left as what it
    is — an optimisation on the sequential path.

    Leaves the file's read position at 0 so the caller can still save it.
    """
    _stem, extension = os.path.splitext(uploaded_file.name or "")
    uploaded_file.seek(0)
    return f"{uuid.uuid4().hex}{extension.lower()}"


class CaseImageUploadThrottle(UserRateThrottle):
    """Upload ceiling, in its OWN bucket.

    ``scope`` and ``rate`` are both set explicitly, for the reason spelled out
    at ``FeedbackRateThrottle``: inheriting ``UserRateThrottle``'s ``"user"``
    scope would share one history list with the global 5000/hour throttle, so a
    caseworker's ordinary API traffic would spend this budget.

    A ceiling exists at all because nothing links an upload to a case. An image
    whose id is never PATCHed onto one is invisible in the app but permanent in
    R2, so a stuck retry loop in the editor bills storage indefinitely with no
    surface that would show it. 120/hour is far above any real editing session.
    """

    scope = "case_image_upload"
    rate = "120/hour"


class CaseImageUploadView(APIView):
    """``POST /api/case-images/`` (multipart) — add an image to the library.

    Multipart fields: ``file`` (required binary), ``title`` (optional; defaults
    to the uploaded filename, which is what makes the image findable later in
    the Wagtail image library).

    Returns ``{id, title, width, height, thumbnail, banner}``, where the last two
    are the responsive payloads for the two ladders the case surfaces use — 201
    when the image was added, 200 when these exact bytes were already in the
    library and the existing row is being handed back. The client treats both the
    same; the distinction is there so the response does not claim to have created
    something it did not.
    """

    permission_classes = [IsCaseImageUploader]
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [CaseImageUploadThrottle]

    # Declared by hand: this is a plain APIView with no serializer to introspect,
    # so drf-spectacular would otherwise drop the endpoint from the published
    # schema entirely — and a multipart endpoint is exactly the one a client
    # cannot guess the shape of.
    @extend_schema(
        operation_id="case_images_create",
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "format": "binary"},
                    "title": {
                        "type": "string",
                        "description": "Library title. Defaults to the filename.",
                    },
                },
                "required": ["file"],
            }
        },
        responses={
            200: _UPLOAD_RESPONSE_SCHEMA,
            201: _UPLOAD_RESPONSE_SCHEMA,
            400: OpenApiResponse(description="Not an image, too large, or absent."),
            403: OpenApiResponse(description="Caseworker role required."),
        },
    )
    def post(self, request):
        uploaded = request.FILES.get("file")
        if uploaded is None:
            return Response(
                {"file": ["A multipart 'file' is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            cleaned = WagtailImageField(required=True).clean(uploaded)
        except DjangoValidationError as exc:
            # Wagtail's messages already name the limit that was hit (format,
            # filesize, pixel count), so surface them rather than a generic 400.
            return Response(
                {"file": list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST
            )

        width, height = cleaned.image.get_size()
        original_name = uploaded.name or "image"

        # Wagtail's own content hash, which it indexes but only ever populates
        # from its admin upload FORMS — an image created in code otherwise
        # carries an empty one, exactly like width/height.
        file_hash = hash_filelike(cleaned)
        cleaned.seek(0)

        image_model = get_image_model()
        # order_by so a library that already holds duplicates from before this
        # endpoint existed resolves to the SAME row every time, rather than
        # whichever one the database happened to return.
        existing = image_model.objects.filter(file_hash=file_hash).order_by("pk").first()
        if existing is not None:
            # These bytes are already in the library, so reuse the row instead of
            # adding a near-identical one: it saves an object in R2 and gives the
            # editor a stable id when a caseworker re-picks the same file.
            #
            # This is an OPTIMISATION, not a safety check, and deliberately not
            # locked or made atomic. It is a check-then-act, so two concurrent
            # uploads of the same file can both miss it — see
            # ``unique_upload_name`` for why losing that race is harmless now
            # (two rows, two distinct objects) rather than the shared-object,
            # delete-one-blanks-the-other hazard it used to be. Serializing it
            # would mean holding a per-hash lock across the upload to R2, which
            # costs more than the redundant object it saves.
            return Response(self._payload(existing), status=status.HTTP_200_OK)

        cleaned.name = unique_upload_name(cleaned)
        image = image_model(
            title=(request.data.get("title") or original_name).strip()[:255],
            file=cleaned,
            width=width,
            height=height,
            file_size=cleaned.size,
            file_hash=file_hash,
            uploaded_by_user=request.user,
        )
        # No transaction: ``save()`` streams the file to R2 before it INSERTs,
        # and there is exactly one row, so a transaction would buy no atomicity
        # while holding a database connection open across the upload.
        image.save()

        return Response(self._payload(image), status=status.HTTP_201_CREATED)

    @staticmethod
    def _payload(image) -> dict:
        """The upload response: the id to PATCH onto a case, plus both ladders.

        Generating the renditions here rather than lazily on first public
        request means the caseworker who uploaded the image pays for them, not
        the first visitor to the case.
        """
        return {
            "id": image.pk,
            "title": image.title,
            "width": image.width,
            "height": image.height,
            "thumbnail": SrcsetRenditionField(specs=CARD_SPECS).to_representation(
                image
            ),
            "banner": SrcsetRenditionField(specs=HERO_SPECS).to_representation(image),
        }
