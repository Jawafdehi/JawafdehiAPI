"""Content-Type pinning for uploads written to object storage.

Guards a bug that only appeared in the deployed image: after WebP renditions
shipped, every ``.webp`` in R2 was served as ``application/octet-stream`` while
``.jpg`` written by the same process was correctly ``image/jpeg``. The write path
resolves ``image/webp`` locally, so the cause was the ``mimetypes`` registry —
which varies by interpreter build and by whether ``/etc/mime.types`` exists in
the base image. These tests pin the header to the extension so it can't depend on
the environment again.
"""

import pytest
from wagtail.images import get_image_model
from wagtail.images.models import Filter
from wagtail.images.tests.utils import get_test_image_file

from jawafdehi_shared.storage import HashedFilenameS3Boto3Storage


@pytest.fixture
def storage():
    # Credentials are never used: nothing here performs a request, these tests
    # only exercise the parameter-building path.
    return HashedFilenameS3Boto3Storage(
        bucket_name="test-bucket", access_key="key", secret_key="secret"
    )


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("case_uploads/abc.webp", "image/webp"),
        ("case_uploads/abc.WEBP", "image/webp"),
        ("case_uploads/abc.avif", "image/avif"),
        ("case_uploads/abc.jpg", "image/jpeg"),
        ("case_uploads/abc.jpeg", "image/jpeg"),
        ("case_uploads/abc.png", "image/png"),
        ("case_uploads/abc.gif", "image/gif"),
        ("case_uploads/abc.svg", "image/svg+xml"),
        ("case_uploads/abc.pdf", "application/pdf"),
    ],
)
def test_media_uploads_carry_their_real_content_type(storage, filename, expected):
    assert storage.get_object_parameters(filename)["ContentType"] == expected


def test_content_type_survives_a_mimetypes_registry_that_knows_nothing(
    storage, monkeypatch
):
    """The actual failure mode: a registry missing the extension.

    Simulates the deployed image by making ``guess_type`` blind. Without the
    pinned map, django-storages would fall through to
    ``application/octet-stream`` — which is exactly what R2 ended up serving.
    """
    monkeypatch.setattr(
        "jawafdehi_shared.storage.mimetypes.guess_type", lambda *a, **kw: (None, None)
    )

    assert (
        storage.get_object_parameters("case_uploads/abc.webp")["ContentType"]
        == "image/webp"
    )


def test_text_uploads_still_get_an_explicit_utf8_charset(storage):
    """Pre-existing behaviour: a bare ``text/*`` makes browsers guess Latin-1 and
    turns Devanagari into mojibake."""
    params = storage.get_object_parameters("case_uploads/notes.txt")

    assert params["ContentType"] == "text/plain; charset=utf-8"


def test_an_unknown_extension_is_left_to_django_storages(storage):
    """Not our job to invent a type — absent from the map and from mimetypes, no
    ContentType is set and django-storages applies its own default."""
    assert "ContentType" not in storage.get_object_parameters("case_uploads/x.zzznope")


@pytest.mark.django_db
def test_wagtail_rendition_filenames_resolve_through_the_map(storage, settings, tmp_path):
    """End-to-end on the real filenames Wagtail produces, since the map is keyed
    on the extension and Wagtail packs the whole filter spec into the name
    (``…fill-1600x900.format-webp.webp``)."""
    settings.MEDIA_ROOT = str(tmp_path)
    image = get_image_model().objects.create(
        title="पशुपतिनाथको जलहरी",
        file=get_test_image_file(filename="jalahari.png", size=(1800, 1013)),
    )

    expected = {
        "fill-800x450|format-webp": "image/webp",
        "fill-1600x900|format-webp": "image/webp",
        "fill-1200x630|format-jpeg|jpegquality-85": "image/jpeg",
    }
    for spec, content_type in expected.items():
        rendition_file = image.generate_rendition_file(Filter(spec=spec))
        key = storage._get_hashed_filename(rendition_file.name)
        assert storage.get_object_parameters(key)["ContentType"] == content_type
