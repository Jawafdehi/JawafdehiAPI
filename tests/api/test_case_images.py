"""Case thumbnail / hero images: renditions, upload, and the write path.

Covers the four things that would silently break the feature:

* the ``srcset`` payload is actually a ladder (ascending, ``w`` descriptors,
  WebP) rather than one URL repeated,
* an upload's storage key is derived from its CONTENT, so two people uploading
  ``photo.png`` do not overwrite each other,
* the image ids round-trip through the RFC-6902 PATCH path onto real columns,
* every read surface (public API, search index, oEmbed) prefers the rendition
  over the deprecated free-text URL.
"""

import hashlib

import pytest
from django.core.cache import caches
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from wagtail.images import get_image_model
from wagtail.images.tests.utils import get_test_image_file

from cases.image_serializers import CARD_SPECS, HERO_SPECS, SrcsetRenditionField
from cases.image_views import content_addressed_name
from cases.models import Case, CaseState, CaseType
from cases.search_index import _build_card
from tests.conftest import create_user_with_role

UPLOAD_URL = "/api/case-images/"


def _isolate_rendition_cache():
    """Drop Wagtail's rendition cache between image fixtures.

    Same trap as ``tests/content/test_article_api.py``: renditions are cached on
    image pk + filter spec, and under pytest every fixture image is pk=1, so
    without this a second test reads the first test's renditions back and
    reports its dimensions.
    """
    for cache in caches.all():
        cache.clear()


@pytest.fixture
def media_root(settings, tmp_path):
    """Write renditions to tmp instead of the checkout's MEDIA_ROOT."""
    settings.MEDIA_ROOT = str(tmp_path)
    _isolate_rendition_cache()
    return tmp_path


@pytest.fixture
def image(db, media_root):
    # Wider than the widest spec in either ladder (1600), so every rendition
    # downscales — Wagtail never upscales, and a narrow source would make the
    # top of the ladder silently duplicate the tier below it.
    return get_image_model().objects.create(
        title="मुद्दाको तस्बिर",
        file=get_test_image_file(filename="case-hero.png", size=(2000, 1125)),
    )


@pytest.fixture
def case(db):
    return Case.objects.create(
        title="परीक्षण मुद्दा",
        slug="image-test-case",
        state=CaseState.PUBLISHED,
        case_type=CaseType.CORRUPTION,
        description="Description",
        short_description="Short",
    )


def _png_upload(name, *, size=(1200, 800)):
    """A real PNG as a multipart upload under the given filename."""
    return SimpleUploadedFile(
        name,
        get_test_image_file(filename=name, size=size).file.getvalue(),
        content_type="image/png",
    )


def _staff_client(db):
    user = create_user_with_role("imguser", "img@example.com", "Caseworker")
    client = APIClient()
    client.force_authenticate(user=user)
    return client


# ---------------------------------------------------------------------------
# SrcsetRenditionField
# ---------------------------------------------------------------------------


def test_srcset_is_an_ascending_webp_ladder(image):
    payload = SrcsetRenditionField(specs=CARD_SPECS).to_representation(image)

    entries = payload["srcset"].split(", ")
    assert len(entries) == len(CARD_SPECS)

    widths = [int(entry.rsplit(" ", 1)[1].removesuffix("w")) for entry in entries]
    assert widths == sorted(widths), "srcset must ascend"
    assert widths == [400, 800, 1200]

    urls = [entry.rsplit(" ", 1)[0] for entry in entries]
    assert len(set(urls)) == len(urls), "each tier must be a distinct file"
    assert all(url.endswith(".webp") for url in urls), (
        "format-webp is load-bearing: PNG renditions are ~8x the bytes"
    )


def test_src_is_the_largest_rendition(image):
    payload = SrcsetRenditionField(specs=CARD_SPECS).to_representation(image)

    # A browser ignoring srcset, and any consumer wanting one URL, gets the top
    # of the ladder rather than an arbitrary tier.
    assert payload["src"] == payload["srcset"].split(", ")[-1].rsplit(" ", 1)[0]
    assert payload["width"] == 1200
    assert payload["height"] == 675


def test_hero_ladder_differs_from_the_card_ladder(image):
    card = SrcsetRenditionField(specs=CARD_SPECS).to_representation(image)
    hero = SrcsetRenditionField(specs=HERO_SPECS).to_representation(image)

    assert card["width"] == 1200
    assert hero["width"] == 1600
    assert card["src"] != hero["src"]


def test_none_serializes_to_none():
    assert SrcsetRenditionField().to_representation(None) is None


# ---------------------------------------------------------------------------
# Case.card_image / Case.hero_image precedence
# ---------------------------------------------------------------------------


def test_each_image_falls_back_to_the_other(case, image):
    case.banner_image = image
    case.save()

    # Only a hero was uploaded: the card shows it rather than a placeholder.
    assert case.card_image == image
    assert case.hero_image == image


def test_thumbnail_wins_for_the_card_and_banner_for_the_hero(case, image, media_root):
    other = get_image_model().objects.create(
        title="अर्को तस्बिर",
        file=get_test_image_file(filename="other.png", size=(2000, 1125)),
    )
    case.thumbnail_image = image
    case.banner_image = other
    case.save()

    assert case.card_image == image
    assert case.hero_image == other


# ---------------------------------------------------------------------------
# Upload endpoint
# ---------------------------------------------------------------------------


def test_upload_creates_an_image_and_returns_both_ladders(db, media_root):
    client = _staff_client(db)

    response = client.post(
        UPLOAD_URL, {"file": _png_upload("photo.png")}, format="multipart"
    )

    assert response.status_code == 201, response.data
    body = response.data
    assert get_image_model().objects.filter(pk=body["id"]).exists()
    assert body["width"] == 1200
    assert body["height"] == 800
    # Prewarmed here so the caseworker pays for the renditions, not the first
    # visitor to the case.
    assert body["thumbnail"]["srcset"].count(",") == len(CARD_SPECS) - 1
    assert body["banner"]["srcset"].count(",") == len(HERO_SPECS) - 1


def test_upload_titles_the_image_with_its_filename(db, media_root):
    client = _staff_client(db)

    response = client.post(
        UPLOAD_URL, {"file": _png_upload("jalahari-case.png")}, format="multipart"
    )

    # The library is browsable by title; a content-hash title would make every
    # uploaded image indistinguishable in the Wagtail chooser.
    assert response.data["title"] == "jalahari-case.png"


def test_same_filename_different_content_does_not_collide(db, media_root):
    """The regression this endpoint exists to prevent.

    ``HashedFilenameS3Boto3Storage`` keys objects on a salted hash of the
    FILENAME and never de-duplicates, so before the content-addressed rename two
    caseworkers uploading ``photo.png`` produced one object and the second
    silently destroyed the first.
    """
    client = _staff_client(db)

    first = client.post(
        UPLOAD_URL,
        {"file": _png_upload("photo.png", size=(1200, 800))},
        format="multipart",
    )
    second = client.post(
        UPLOAD_URL,
        {"file": _png_upload("photo.png", size=(900, 600))},
        format="multipart",
    )

    assert first.status_code == 201 and second.status_code == 201
    assert first.data["id"] != second.data["id"]

    image_model = get_image_model()
    first_name = image_model.objects.get(pk=first.data["id"]).file.name
    second_name = image_model.objects.get(pk=second.data["id"]).file.name
    assert first_name != second_name

    # And the first image is still readable — the point of the whole exercise.
    assert image_model.objects.get(pk=first.data["id"]).width == 1200


def test_content_addressed_name_is_the_sha256_of_the_bytes():
    upload = _png_upload("anything.PNG")
    expected = hashlib.sha256(upload.read()).hexdigest()
    upload.seek(0)

    name = content_addressed_name(upload)

    assert name == f"{expected}.png", "extension is normalized to lowercase"
    assert upload.tell() == 0, "the file must be rewound for the caller to save it"


def test_identical_bytes_get_the_same_name_whatever_they_were_called():
    """The other half of content-addressing: same bytes, same name.

    Asserted on the naming function rather than end-to-end, because whether two
    identical uploads then collapse to ONE stored object is a property of the
    storage backend. ``HashedFilenameS3Boto3Storage`` (production) de-duplicates;
    ``FileSystemStorage`` (what the suite runs on) appends a suffix instead.
    """
    payload = get_test_image_file(filename="dup.png", size=(800, 600)).file.getvalue()

    first = SimpleUploadedFile("a.png", payload, content_type="image/png")
    second = SimpleUploadedFile("b-totally-different-name.png", payload, "image/png")

    assert content_addressed_name(first) == content_addressed_name(second)


def test_upload_requires_the_caseworker_role(db, media_root):
    anonymous = APIClient()
    assert (
        anonymous.post(
            UPLOAD_URL, {"file": _png_upload("photo.png")}, format="multipart"
        ).status_code
        in (401, 403)
    )

    outsider = APIClient()
    outsider.force_authenticate(
        user=create_user_with_role("nobody", "nobody@example.com", "Public")
    )
    assert (
        outsider.post(
            UPLOAD_URL, {"file": _png_upload("photo.png")}, format="multipart"
        ).status_code
        == 403
    )


def test_upload_rejects_a_non_image(db, media_root):
    client = _staff_client(db)

    response = client.post(
        UPLOAD_URL,
        {"file": SimpleUploadedFile("notes.png", b"not an image", "image/png")},
        format="multipart",
    )

    assert response.status_code == 400
    assert "file" in response.data


def test_upload_requires_a_file(db, media_root):
    response = _staff_client(db).post(UPLOAD_URL, {}, format="multipart")

    assert response.status_code == 400
    assert "file" in response.data


# ---------------------------------------------------------------------------
# Write path: image ids through the RFC-6902 PATCH
# ---------------------------------------------------------------------------


def test_patch_sets_and_clears_the_image_ids(case, image, media_root):
    client = _staff_client(case._state.db)
    url = f"/api/cases/{case.slug}/"

    set_response = client.patch(
        url,
        [
            {"op": "replace", "path": "/thumbnail_image_id", "value": image.pk},
            {"op": "replace", "path": "/banner_image_id", "value": image.pk},
        ],
        format="json",
    )
    assert set_response.status_code == 200, set_response.data

    case.refresh_from_db()
    assert case.thumbnail_image_id == image.pk
    assert case.banner_image_id == image.pk

    clear_response = client.patch(
        url,
        [{"op": "replace", "path": "/thumbnail_image_id", "value": None}],
        format="json",
    )
    assert clear_response.status_code == 200, clear_response.data

    case.refresh_from_db()
    assert case.thumbnail_image_id is None
    assert case.banner_image_id == image.pk


def test_patch_rejects_an_unknown_image_id(case, media_root):
    response = _staff_client(case._state.db).patch(
        f"/api/cases/{case.slug}/",
        [{"op": "replace", "path": "/thumbnail_image_id", "value": 999999}],
        format="json",
    )

    # 422, not a 500 from an IntegrityError deep in the bulk UPDATE.
    assert response.status_code == 422
    assert "thumbnail_image_id" in response.data


# ---------------------------------------------------------------------------
# Read surfaces
# ---------------------------------------------------------------------------


def test_public_case_api_returns_the_srcset(case, image, media_root):
    case.thumbnail_image = image
    case.save()

    data = APIClient().get(f"/api/cases/{case.slug}/").data

    assert data["thumbnail"]["srcset"].count(",") == len(CARD_SPECS) - 1
    assert data["banner"]["width"] == 1600, "hero falls back to the card image"
    assert data["thumbnail_image_id"] == image.pk


def test_public_case_api_returns_null_images_when_none_uploaded(case):
    data = APIClient().get(f"/api/cases/{case.slug}/").data

    # Null rather than absent, so the client can fall back to the deprecated URL.
    assert data["thumbnail"] is None
    assert data["banner"] is None


def test_search_card_carries_the_rendition(case, image, media_root):
    case.thumbnail_image = image
    case.save()

    card = _build_card(
        case,
        slug=case.slug,
        title=case.title,
        short=case.short_description,
        tags=[],
        case_type=case.case_type,
        case_status="ongoing",
        entities=[],
    )

    assert card["thumbnail"]["width"] == 1200
    assert card["thumbnail"]["srcset"].count(",") == len(CARD_SPECS) - 1


def test_search_card_tolerates_a_stand_in_without_an_image():
    """``_build_card`` shapes whatever it is handed; the doc-shape tests rely on it."""

    class Stub:
        slug = "x"
        title = "x"

    card = _build_card(
        Stub(),
        slug="x",
        title="x",
        short=None,
        tags=[],
        case_type="CORRUPTION",
        case_status="ongoing",
        entities=[],
    )

    assert card["thumbnail"] is None


def test_oembed_prefers_the_rendition_over_the_free_text_url(case, image, media_root):
    case.thumbnail_image = image
    case.thumbnail_url = "https://example.com/legacy.png"
    case.save()

    data = APIClient().get(
        "/api/oembed/", {"url": f"https://jawafdehi.org/case/{case.slug}"}
    ).data

    assert data["thumbnail_url"].endswith(".webp")
    assert data["thumbnail_width"] == 800
    assert data["thumbnail_height"] == 450


def test_oembed_falls_back_to_the_free_text_url(case):
    case.thumbnail_url = "https://example.com/legacy.png"
    case.save()

    data = APIClient().get(
        "/api/oembed/", {"url": f"https://jawafdehi.org/case/{case.slug}"}
    ).data

    assert data["thumbnail_url"] == "https://example.com/legacy.png"
    # No rendition means no dimensions to declare.
    assert data["thumbnail_width"] is None
