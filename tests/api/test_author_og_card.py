"""The composed Open Graph share card for an author page.

Covers the endpoint's contract (a 1200x630 JPEG, public, 404 under the same rule
as the profile), the cache window the frontend Worker relies on, and the shaping
guard that stops a Nepali name being published with detached matras.

The renderer itself is exercised directly rather than only through the view, so a
layout change that crashes on a missing photo or an over-long name fails here and
not in a link preview.
"""

from io import BytesIO

import pytest
from django.contrib.auth import get_user_model
from PIL import Image
from rest_framework.test import APIClient

from cases import og_cards
from cases.models import AuthorProfile

User = get_user_model()

URL = "/api/authors/{}/og-card.jpg"


def _profile(**fields) -> AuthorProfile:
    user = User.objects.create_user(
        username=fields.pop("username", "rujit"),
        first_name=fields.pop("first_name", "Rujit"),
        last_name=fields.pop("last_name", "Kafle"),
    )
    defaults = dict(
        slug="rujit-kafle",
        name_ne="रुजित काफ्ले",
        title="Caseworker",
        has_public_page=True,
    )
    defaults.update(fields)
    return AuthorProfile.objects.create(user=user, **defaults)


@pytest.fixture(autouse=True)
def no_photo_fetch(monkeypatch):
    """Never reach the network from a test.

    The photo is a separate concern from the card, and a suite that fetches a
    real headshot fails whenever the CDN hiccups. Tests that want a portrait
    inject one.
    """
    monkeypatch.setattr(og_cards, "fetch_photo", lambda url: None)


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_card_is_a_1200x630_jpeg():
    profile = _profile()

    response = APIClient().get(URL.format(profile.slug))

    assert response.status_code == 200
    assert response["Content-Type"] == "image/jpeg"
    with Image.open(BytesIO(response.content)) as card:
        # The ratio every unfurler lays out summary_large_image at. A square or
        # arbitrary-sized image here is the bug this whole endpoint exists to fix.
        assert card.size == (1200, 630)
        assert card.format == "JPEG"


@pytest.mark.django_db
def test_card_is_public():
    """No auth. A crawler is anonymous by definition — Facebook and WhatsApp
    fetch this with no cookie and no token."""
    profile = _profile()

    assert APIClient().get(URL.format(profile.slug)).status_code == 200


@pytest.mark.django_db
def test_card_404s_for_an_unpublished_profile():
    """Same rule as the profile page itself: a profile row is auto-created on
    first credit, so an unpublished one is an empty placeholder, not a page."""
    profile = _profile(has_public_page=False)

    assert APIClient().get(URL.format(profile.slug)).status_code == 404


@pytest.mark.django_db
def test_card_404s_for_an_unknown_slug():
    assert APIClient().get(URL.format("nobody-at-all")).status_code == 404


@pytest.mark.django_db
def test_card_is_cached_for_a_day_in_shared_caches():
    """The Worker proxies this and caches it for a day; `s-maxage` is what tells
    it (and Cloudflare) that, without pinning the bytes in every scraper's own
    store for a day as a plain `max-age` would."""
    profile = _profile()

    response = APIClient().get(URL.format(profile.slug))

    assert "s-maxage=86400" in response["Cache-Control"]
    assert "public" in response["Cache-Control"]


@pytest.mark.django_db
def test_card_renders_when_the_photo_cannot_be_fetched(monkeypatch):
    """A dead photo host must not 500 a share preview. The card drops the
    portrait column and sets the name full-width instead."""
    profile = _profile(photo_url="https://example.invalid/gone.webp")
    monkeypatch.setattr(og_cards, "fetch_photo", lambda url: None)

    response = APIClient().get(URL.format(profile.slug))

    assert response.status_code == 200
    with Image.open(BytesIO(response.content)) as card:
        assert card.size == (1200, 630)


@pytest.mark.django_db
def test_card_503s_rather_than_publish_an_unshaped_nepali_name(monkeypatch):
    """The image is missing libfribidi.

    503 (and the Worker falls back to the generic site banner) beats 200 with a
    name whose matras have come apart: a generic card is merely unhelpful, a
    mangled name is wrong and is then cached in every chat it was shared to.
    """
    profile = _profile()
    monkeypatch.setattr(og_cards.features, "check", lambda name: False)

    assert APIClient().get(URL.format(profile.slug)).status_code == 503


# ---------------------------------------------------------------------------
# The renderer
# ---------------------------------------------------------------------------


def _render(**kwargs) -> Image.Image:
    defaults = dict(display_name="Rujit Kafle", name_ne="रुजित काफ्ले", title="Caseworker")
    defaults.update(kwargs)
    return Image.open(BytesIO(og_cards.render_author_card(**defaults)))


def test_renderer_needs_no_photo_and_no_role():
    """Both are optional on the model — a profile is auto-created blank."""
    with _render(name_ne="", title="", photo=None) as card:
        assert card.size == (1200, 630)


def test_renderer_survives_an_absurdly_long_name():
    """Names are user data. The layout shrinks to fit and ellipsizes as a last
    resort; what it must never do is overrun the card or raise."""
    with _render(display_name="Bahadur " * 20, name_ne="काफ्ले " * 20) as card:
        assert card.size == (1200, 630)


def test_renderer_shapes_devanagari_when_raqm_is_present():
    """Guards the actual failure mode: unshaped Devanagari renders WIDER than
    shaped, because the conjuncts and matras that should combine are laid out as
    separate advances instead.

    Skipped where libfribidi is absent, because a developer machine without it
    proves nothing about the deployed image — the Dockerfile is what guarantees
    that, and the 503 above is what protects it.
    """
    if not og_cards.features.check("raqm"):
        pytest.skip("Pillow has no Raqm here; the image installs libfribidi0")

    face = og_cards._devanagari(44)
    # काफ्ले carries a conjunct (फ्ल) and a vowel sign; shaped, it is narrower
    # than the sum of its unjoined parts.
    assert face.getlength("काफ्ले") < face.getlength("क") + face.getlength("ा") + face.getlength(
        "फ"
    ) + face.getlength("्") + face.getlength("ल") + face.getlength("े")


def test_devanagari_weight_axis_is_not_transposed():
    """The axis order is (Weight, Width), the REVERSE of the filename's
    "_wdth,wght". Getting it backwards sets weight to 100 (Thin) and silently
    renders a wispy wordmark instead of a bold one — no error, just a wrong card.
    """
    axes = og_cards._devanagari(42).get_variation_axes()
    assert [axis["name"] for axis in axes] == [b"Weight", b"Width"]
    # Bold really is heavier than Thin: the two must not render identically,
    # which is what a transposed (clamped) axis would produce.
    assert og_cards._devanagari(42, 700).getlength("जवाफदेही") != pytest.approx(
        og_cards._devanagari(42, 100).getlength("जवाफदेही")
    )
