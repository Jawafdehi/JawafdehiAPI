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
from django.core.cache import cache
from PIL import Image
from rest_framework.test import APIClient

from cases import api_views, og_cards
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
def clear_card_cache():
    """The view memoizes rendered bytes, and locmem outlives a single test.

    Without this, a test that stubs out shaping could get a cache hit from an
    earlier test's render and pass for the wrong reason.
    """
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def no_photo_fetch(monkeypatch):
    """Never reach the network from a test.

    The photo is a separate concern from the card, and a suite that fetches a
    real headshot fails whenever the CDN hiccups.

    Patched on ``cases.api_views``, NOT on ``cases.og_cards``: the view does
    ``from .og_cards import fetch_photo``, which binds the function into its own
    module namespace, so rebinding the name on og_cards leaves the view calling
    the original. That is not a style point — patching the wrong one let the
    photo test make a real request to a bogus host and pass because the request
    failed, which is the right answer for the wrong reason.
    """
    monkeypatch.setattr(api_views, "fetch_photo", lambda url: None)


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
@pytest.mark.parametrize(
    "accept",
    [
        # What the frontend Worker actually sends.
        "image/jpeg,image/*",
        # What a browser sends for an <img>.
        "image/avif,image/webp,image/png,image/svg+xml,*/*;q=0.8",
        "image/*",
        "*/*",
    ],
)
def test_card_serves_clients_that_ask_for_an_image(accept):
    """Regression: this endpoint answered 406 to every real caller.

    As a DRF APIView it negotiated content in ``initial()`` before the handler
    ran, and its renderers advertised only ``application/json`` — so any client
    asking for an image was refused. Every test passed anyway, because DRF's
    APIClient defaults to ``Accept: */*``, which matches anything. The Worker
    would have fallen back to the generic banner forever and nothing would have
    said why.
    """
    profile = _profile()

    response = APIClient().get(URL.format(profile.slug), HTTP_ACCEPT=accept)

    assert response.status_code == 200, f"Accept: {accept} got {response.status_code}"
    assert response["Content-Type"] == "image/jpeg"


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
    profile = _profile(photo_url="https://jawafdehi.org/assets/teammembers/gone.webp")
    monkeypatch.setattr(api_views, "fetch_photo", lambda url: None)

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


# ---------------------------------------------------------------------------
# fetch_photo: a public endpoint fetching a URL from the database
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # SSRF targets. This endpoint is public and unauthenticated, and it
        # fetches from inside the cluster, so an unconstrained host is a way to
        # reach things nothing outside should reach.
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://127.0.0.1/",
        "https://localhost/x.png",
        "https://10.0.0.5/x.png",
        "https://kubernetes.default.svc/x.png",
        "https://evil.example.com/x.png",
        # Not https at all.
        "http://jawafdehi.org/assets/teammembers/rujit.webp",
        "file:///etc/passwd",
        "",
    ],
)
def test_fetch_photo_refuses_anything_off_the_allowlist(url, monkeypatch):
    """Never reaches the network for a host we do not serve photos from.

    Asserted by making any request at all a hard failure, so this cannot pass
    merely because the address happened to be unroutable from CI.
    """

    def explode(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError(f"fetch_photo tried to request {url}")

    monkeypatch.setattr(og_cards.requests, "get", explode)

    assert og_cards.fetch_photo(url) is None


def test_fetch_photo_does_not_follow_redirects(monkeypatch):
    """An allowed host must not be able to bounce us onward to an internal
    address, so a redirect fails the fetch rather than being followed."""
    seen = {}

    class Response:
        status_code = 302
        headers = {"Location": "https://169.254.169.254/"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            raise og_cards.requests.HTTPError("302")

    def capture(url, **kwargs):
        seen.update(kwargs)
        return Response()

    monkeypatch.setattr(og_cards.requests, "get", capture)

    assert og_cards.fetch_photo("https://jawafdehi.org/assets/a.webp") is None
    assert seen["allow_redirects"] is False


def test_fetch_photo_rejects_a_decompression_bomb(monkeypatch):
    """Compressed size is not the bound that matters.

    A 20 KB PNG can declare 60000x60000 and expand to gigabytes. The pixel count
    is checked off the header, before load() allocates anything.
    """
    bomb = BytesIO()
    Image.new("RGB", (1, 1)).save(bomb, format="PNG")
    payload = bomb.getvalue()

    class Raw:
        @staticmethod
        def read(n, decode_content=False):
            return payload

    class Response:
        raw = Raw()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

    monkeypatch.setattr(og_cards.requests, "get", lambda url, **kwargs: Response())
    # Report an enormous size from the header without building the pixels.
    monkeypatch.setattr(
        og_cards, "PHOTO_MAX_PIXELS", 0, raising=True
    )

    assert og_cards.fetch_photo("https://jawafdehi.org/assets/a.png") is None


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
