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

import hashlib
import os

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from wagtail.images import get_image_model
from wagtail.images.fields import WagtailImageField

from .image_serializers import CARD_SPECS, HERO_SPECS, SrcsetRenditionField
from .permissions import IsCaseImageUploader

#: Chunk size for the content hash. Matches Django's own ``UploadedFile.chunks``
#: default so a large upload is never fully resident in memory here.
_HASH_CHUNK_BYTES = 64 * 1024


def content_addressed_name(uploaded_file) -> str:
    """Rename an upload to ``<sha256 of its bytes><ext>``.

    This is a correctness fix, not a tidiness one. ``default_storage`` is
    ``HashedFilenameS3Boto3Storage``, which derives the object key from a salted
    hash of the *filename* and — deliberately, see its ``get_available_name`` —
    never de-duplicates. Wagtail hands it ``original_images/<filename>``, so two
    different images uploaded as ``photo.jpg`` produce one key and the second
    silently overwrites the first. That behaviour is right for the document
    ingestion path it was built for, where the name is a stable identifier; it is
    data loss for images a person picked off their desktop, where ``photo.jpg``
    and ``IMG_0001.jpg`` are the common case.

    Hashing the bytes here restores the property the storage layer assumes:
    distinct content gets a distinct name. Identical content still collapses to
    one key, which is a free de-duplication rather than a hazard.

    Leaves the file's read position at 0 so the caller can still save it.
    """
    digest = hashlib.sha256()
    for chunk in uploaded_file.chunks(_HASH_CHUNK_BYTES):
        digest.update(chunk)
    uploaded_file.seek(0)

    _stem, extension = os.path.splitext(uploaded_file.name or "")
    return f"{digest.hexdigest()}{extension.lower()}"


class CaseImageUploadView(APIView):
    """``POST /api/case-images/`` (multipart) — add an image to the library.

    Multipart fields: ``file`` (required binary), ``title`` (optional; defaults
    to the uploaded filename, which is what makes the image findable later in
    the Wagtail image library).

    Returns 201 with ``{id, title, width, height, thumbnail, banner}``, where the
    last two are the responsive payloads for the two ladders the case surfaces
    use. Generating them here rather than lazily on first public request means
    the caseworker who uploaded the image pays for the renditions, not the first
    visitor to the case.
    """

    permission_classes = [IsCaseImageUploader]
    parser_classes = [MultiPartParser, FormParser]

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
        cleaned.name = content_addressed_name(cleaned)

        image_model = get_image_model()
        image = image_model(
            title=(request.data.get("title") or original_name).strip()[:255],
            file=cleaned,
            width=width,
            height=height,
            uploaded_by_user=request.user,
        )
        with transaction.atomic():
            image.save()

        return Response(
            {
                "id": image.pk,
                "title": image.title,
                "width": image.width,
                "height": image.height,
                "thumbnail": SrcsetRenditionField(specs=CARD_SPECS).to_representation(
                    image
                ),
                "banner": SrcsetRenditionField(specs=HERO_SPECS).to_representation(
                    image
                ),
            },
            status=status.HTTP_201_CREATED,
        )
