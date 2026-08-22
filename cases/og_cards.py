# SPDX-License-Identifier: Hippocratic-3.0
"""Composed Open Graph share cards, rendered with Pillow.

Sharing an author page used to unfurl the site-wide banner, so every author
looked identical in a WhatsApp, Facebook or Slack preview and the person's name
appeared nowhere in the image. This renders a real card instead: their
photograph, their name in both scripts, and their role, on the same
navy/crimson ground as the site banner.

Rendered HERE rather than baked into the frontend as committed files so that a
newly credited author gets a correct card with no rebuild and no commit. The
frontend Worker proxies this endpoint under its own origin and caches it for a
day, so the cost is one render per author per day.

Why not the obvious alternatives:

  * Pointing ``og:image`` at the author's existing headshot. Those are 504x504
    WebP. WebP unfurls unreliably on WhatsApp and LinkedIn — the same reason the
    CMS generates a JPEG ``og_image`` rendition for articles rather than reusing
    the WebP thumbnail — and a square image in a ``summary_large_image`` card is
    centre-cropped to 1.91:1, i.e. a horizontal band across the middle of the
    face.
  * Composing in the Worker with satori/resvg. Satori has no complex-script
    shaping, so every Devanagari name would render with its conjuncts and matras
    broken. Shaping is the whole reason this lives in Python.

DEVANAGARI SHAPING IS A DEPLOY DEPENDENCY. Pillow vendors Raqm but ``dlopen``s
its two backends at runtime: libharfbuzz (bundled inside the Pillow wheel) and
**libfribidi (NOT bundled)**. Without libfribidi on the image,
``PIL.features.check("raqm")`` is False and Pillow falls back to unshaped
rendering — the Nepali names come out with their parts detached, silently, with
no error. The Dockerfile installs ``libfribidi0`` for exactly this reason; do not
drop it. ``render_author_card`` refuses to render unshaped rather than emit a
card with a mangled name.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, features

logger = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent / "assets/og"
FONTS = ASSETS / "fonts"

# The card's intrinsic size. 1200x630 is the ratio every unfurler lays out
# `summary_large_image` at.
WIDTH, HEIGHT = 1200, 630

# Brand palette, mirroring the frontend's src/index.css. Navy #0E1F3A is the
# ground and crimson #B5242C the accent — the same two colours as the site
# banner at /assets/social-preview.png.
NAVY = (14, 31, 58)
CRIMSON = (181, 36, 44)
WHITE = (255, 255, 255)
# Muted steps on navy, both clearing 4.5:1 against it.
MUTED = (169, 186, 208)
DIM = (143, 163, 190)

# The canonical descriptor, verbatim from the frontend's SITE_DESCRIPTION, with
# the ASCII apostrophe replaced by a typographic one because this is set type
# rather than a meta tag. Do NOT paraphrase or shorten it: the branding audit
# found five paraphrases of this line live simultaneously.
DESCRIPTOR_RUNS = (
    ("Nepal’s Permanent ", WHITE),
    ("Corruption Case", CRIMSON),
    (" Archive", WHITE),
)
WORDMARK_NE = "जवाफदेही"
WORDMARK_EN = "JAWAFDEHI"
# How the descriptor breaks when it is set as the site banner's headline. The
# break falls BEFORE "Corruption" so the crimson run sits whole on one line.
DESCRIPTOR_LINE_1 = "Nepal’s Permanent"
DESCRIPTOR_LINE_2_RUNS = (("Corruption Case", CRIMSON), (" Archive", WHITE))
TAGLINE = "— who, what, and when."
DOMAIN = "jawafdehi.org"

# Layout. These are the composition, not a grid system, so they are literal.
LEFT_EDGE = 88
RIGHT_EDGE = WIDTH - 72
PHOTO_SIZE = 300
PHOTO_X = 88
TEXT_X = 452
# Ring around the portrait, so a dark-jacketed photograph does not bleed into
# the navy ground and read as a blob.
RING = 4

# The dot field, the same device as the frontend's page heroes (PageHeroBackdrop
# in src/components/ui/page-hero.tsx), which draws a radial-gradient dot at
# 0.75px on an 18px grid damped to 0.22 opacity.
#
# The numbers are NOT carried across at 1:1, because the card is not viewed at
# 1:1. An unfurled preview renders around 320-400 CSS px wide, so a 1200px card
# is downscaled ~3x: an 18px grid would land at 6px on screen, where the dots
# alias into a flat haze and read as JPEG noise. The grid is scaled by that same
# factor to keep the ON-SCREEN rhythm the site has. The alpha is higher than the
# site's effective ~3% because that value assumes a light ground; on navy it is
# invisible.
DOT_SPACING = 54
DOT_RADIUS = 2.4
DOT_ALPHA = 30

# Bound the outbound fetch of an author's photograph. It is a third-party origin
# from this process's point of view, and a card is not worth holding a worker on.
PHOTO_TIMEOUT = 6
# A headshot is tens of KB. Anything far past that is not a portrait, and
# decoding it would be a denial-of-service vector via a hostile photo_url.
PHOTO_MAX_BYTES = 8 * 1024 * 1024


class ShapingUnavailable(RuntimeError):
    """Raised when Pillow cannot shape complex text.

    A card whose Nepali name is rendered with detached matras is worse than no
    card: the caller can fall back to the site banner, but it cannot un-publish a
    mangled name from a hundred chat previews.
    """


@lru_cache(maxsize=None)
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


def _vesper(size: int, weight: str = "Bold") -> ImageFont.FreeTypeFont:
    """Vesper Libre — the display face, bilingual Latin + Devanagari, and the
    face the site banner already uses for its editorial line."""
    return _font(f"VesperLibre-{weight}.ttf", size)


@lru_cache(maxsize=None)
def _devanagari(size: int, weight: int = 700) -> ImageFont.FreeTypeFont:
    """Noto Sans Devanagari, the frontend's app face for Nepali.

    A variable font, so weight is set on the axis. The axis ORDER is
    (Weight, Width) — the REVERSE of what the file is named
    ("...VariableFont_wdth,wght.ttf"). Trusting the filename sets weight to the
    width value: [100, 700] renders Thin with the width clamped, which looks
    like a font-loading bug rather than a wrong argument. Read the order off
    ``get_variation_axes()``, not the name.
    """
    face = ImageFont.truetype(
        str(FONTS / "NotoSansDevanagari-VariableFont_wdth,wght.ttf"), size
    )
    try:
        face.set_variation_by_axes([float(weight), 100.0])
    except (OSError, AttributeError):  # pragma: no cover - build-dependent
        # A Pillow without variation support still renders at the default
        # weight, which is legible — just lighter than intended.
        logger.warning("Pillow cannot set variation axes; wordmark will be light")
    return face


def _draw_runs(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    runs: tuple[tuple[str, tuple[int, int, int]], ...],
    face: ImageFont.FreeTypeFont,
) -> None:
    """Draw one line made of differently-coloured runs, advancing by each run's
    measured width.

    Runs are split at spaces so no kerning pair is broken by a colour change.
    """
    for text, colour in runs:
        draw.text((x, y), text, font=face, fill=colour)
        x += round(face.getlength(text))


def _dot_field(size: tuple[int, int]) -> Image.Image:
    """The full-bleed dot grid, as an RGBA layer to composite over the ground.

    Supersampled and downscaled because PIL draws no antialiased ellipse, and a
    2.4px hard-edged dot repeated 300 times is visibly square.
    """
    scale = 4
    w, h = size
    layer = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    radius = DOT_RADIUS * scale
    # Start half a cell in so the field is inset symmetrically rather than
    # clipping a half-dot against every edge.
    offset = DOT_SPACING / 2
    y = offset
    while y < h:
        x = offset
        while x < w:
            cx, cy = x * scale, y * scale
            draw.ellipse(
                (cx - radius, cy - radius, cx + radius, cy + radius),
                fill=(*WHITE, DOT_ALPHA),
            )
            x += DOT_SPACING
        y += DOT_SPACING
    return layer.resize((w, h), Image.LANCZOS)


def _ground() -> Image.Image:
    card = Image.new("RGBA", (WIDTH, HEIGHT), (*NAVY, 255))
    card.alpha_composite(_dot_field((WIDTH, HEIGHT)))
    return card


def _logo_mark(size: int) -> Image.Image:
    """The cylinder-and-flag mark on a white rounded panel.

    The mark is a navy cylinder with red bands, so on the navy ground its
    outline and its whole top face disappear. It needs a light panel behind it —
    which is also how it is presented on the site banner.
    """
    scale = 4
    radius = round(size * 0.22)
    panel = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    ImageDraw.Draw(panel).rounded_rectangle(
        (0, 0, size * scale - 1, size * scale - 1),
        radius=radius * scale,
        fill=(*WHITE, 255),
    )
    panel = panel.resize((size, size), Image.LANCZOS)

    with Image.open(ASSETS / "logo-mark.png") as raw:
        inner = round(size * 0.76)
        mark = raw.convert("RGBA").resize((inner, inner), Image.LANCZOS)
    offset = (size - inner) // 2
    panel.alpha_composite(mark, (offset, offset))
    return panel


def _draw_masthead(card: Image.Image, x: int, y: int) -> int:
    """The masthead: mark, जवाफदेही, and the descriptor. Returns the y below it."""
    draw = ImageDraw.Draw(card)
    mark_size = 92
    card.alpha_composite(_logo_mark(mark_size), (x, y))

    text_x = x + mark_size + 28
    draw.text((text_x, y - 4), WORDMARK_NE, font=_devanagari(42), fill=WHITE)
    # Set large enough to be read rather than skimmed: this line, not the
    # wordmark, is what tells a stranger seeing the card in a chat what the site
    # is.
    _draw_runs(draw, text_x, y + 48, DESCRIPTOR_RUNS, _vesper(31, "Medium"))
    return y + mark_size


def render_site_card() -> bytes:
    """The site-wide banner as PNG bytes — the card every page that is not a
    case, update or author page unfurls with.

    Lives here, next to the author card, so both share one dot field, one logo
    treatment and one descriptor rather than drifting apart in two repos. It is
    NOT served from an endpoint, though: the frontend commits the output as a
    static asset, because it is also the FALLBACK the Worker uses when this
    service cannot answer, and a fallback that depends on the service being up is
    not a fallback. Regenerate it with `manage.py render_og_site_card` when the
    branding changes.

    PNG rather than JPEG: it carries no photograph, only flat colour and type.
    """
    card = _ground()
    draw = ImageDraw.Draw(card)

    mark_size = 272
    card.alpha_composite(_logo_mark(mark_size), (90, (HEIGHT - mark_size) // 2))

    x = 437
    draw.text((x, 92), WORDMARK_NE, font=_devanagari(84), fill=WHITE)
    # Letterspaced by inserting spaces: PIL has no tracking control, and drawing
    # glyph by glyph instead would lose the font's kerning.
    draw.text((x, 210), " ".join(WORDMARK_EN), font=_vesper(33, "Medium"), fill=WHITE)
    draw.rectangle((x, 290, x + 138, 295), fill=CRIMSON)

    headline = _vesper(44, "Bold")
    draw.text((x, 336), DESCRIPTOR_LINE_1, font=headline, fill=WHITE)
    _draw_runs(draw, x, 412, DESCRIPTOR_LINE_2_RUNS, headline)
    draw.text((x, 494), TAGLINE, font=_vesper(30, "Regular"), fill=MUTED)

    domain_font = _vesper(29, "Medium")
    draw.text(
        (RIGHT_EDGE - domain_font.getlength(DOMAIN), HEIGHT - 82),
        DOMAIN,
        font=domain_font,
        fill=MUTED,
    )

    buffer = BytesIO()
    card.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _circular(photo: Image.Image, size: int) -> Image.Image:
    """Centre-crop to a circle at `size`, matching the round avatar the author
    page itself renders (`rounded-full` + `object-cover`)."""
    w, h = photo.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    square = photo.convert("RGB").crop((left, top, left + side, top + side))
    square = square.resize((size, size), Image.LANCZOS)

    # Supersample the mask, then downscale: PIL has no antialiased ellipse, and a
    # hard-edged circle on navy shows visible stair-stepping at this size.
    scale = 4
    mask = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * scale - 1, size * scale - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(square, (0, 0), mask)
    return out


def _fit_text(
    text: str,
    build,
    size: int,
    max_width: int,
    min_size: int,
) -> tuple[ImageFont.FreeTypeFont, str]:
    """Shrink the font until the line fits, then ellipsize if it still does not.

    Names are user data. Shrinking first keeps the whole name; the ellipsis is a
    last resort, because a truncated name is worse than a slightly smaller one.
    """
    for candidate in range(size, min_size - 1, -2):
        face = build(candidate)
        if face.getlength(text) <= max_width:
            return face, text
    face = build(min_size)
    trimmed = text
    while trimmed and face.getlength(f"{trimmed}…") > max_width:
        trimmed = trimmed[:-1]
    return face, f"{trimmed}…" if trimmed else text


def fetch_photo(url: str) -> Image.Image | None:
    """The author's headshot, or None when it cannot be had.

    Never raises: a card without the portrait is a reasonable card, and an
    unreachable photo host must not turn into a 500 on a share preview.
    """
    if not url or not url.startswith("https://"):
        return None
    try:
        response = requests.get(
            url,
            timeout=PHOTO_TIMEOUT,
            # Cloudflare fronts the origin these are served from and 403s a
            # default client UA.
            headers={"User-Agent": "jawafdehi-og-cards/1.0", "Accept": "image/*"},
            stream=True,
        )
        response.raise_for_status()
        payload = response.raw.read(PHOTO_MAX_BYTES + 1, decode_content=True)
        if len(payload) > PHOTO_MAX_BYTES:
            logger.warning("author photo over %d bytes, skipping: %s", PHOTO_MAX_BYTES, url)
            return None
        image = Image.open(BytesIO(payload))
        image.load()
        return image
    except (requests.RequestException, OSError, ValueError) as err:
        logger.warning("author photo unreadable (%s): %s", url, err)
        return None


def render_author_card(
    *,
    display_name: str,
    name_ne: str = "",
    title: str = "",
    photo: Image.Image | None = None,
) -> bytes:
    """An author's card as JPEG bytes: masthead, portrait, name, role.

    Deliberately NOT on the card: the number of cases the person has written.
    The card is cached for a day at the edge, so a count would be visibly wrong
    to anyone who shared the page shortly after a new case went live, and
    nothing would flag it as stale.

    Raises ShapingUnavailable when Pillow cannot shape Devanagari and there is a
    Nepali name to shape.
    """
    name = (display_name or "").strip()
    name_ne = (name_ne or "").strip()
    title = (title or "").strip()

    if name_ne and not features.check("raqm"):
        raise ShapingUnavailable(
            "Pillow has no Raqm (libfribidi missing from the image); a Devanagari "
            "name would render with detached matras"
        )

    card = _ground()
    draw = ImageDraw.Draw(card)

    photo_y = _draw_masthead(card, LEFT_EDGE, 52) + 74

    if photo is not None:
        avatar = _circular(photo, PHOTO_SIZE)
        # Drawn as a filled circle BEHIND the avatar rather than an outline on
        # top, so the photo's own edge stays clean.
        draw.ellipse(
            (
                PHOTO_X - RING,
                photo_y - RING,
                PHOTO_X + PHOTO_SIZE + RING,
                photo_y + PHOTO_SIZE + RING,
            ),
            fill=MUTED,
        )
        card.alpha_composite(avatar, (PHOTO_X, photo_y))
        text_x = TEXT_X
    else:
        # No photograph on record: drop the left column entirely rather than
        # leave a placeholder silhouette, and let the name run the full width.
        text_x = LEFT_EDGE

    max_width = RIGHT_EDGE - text_x

    name_font, name_text = _fit_text(
        name, lambda s: _vesper(s, "Bold"), 64, max_width, 38
    )
    # The Nepali name is skipped when unset or identical to the Latin one — a
    # duplicated line looks like a rendering bug.
    show_ne = bool(name_ne) and name_ne != name
    ne_font, ne_text = (
        _fit_text(name_ne, _devanagari, 44, max_width, 30) if show_ne else (None, "")
    )
    title_font, title_text = (
        _fit_text(title, lambda s: _vesper(s, "Medium"), 30, max_width, 22)
        if title
        else (None, "")
    )

    # Leading between the two names. Devanagari sets tall — the matras sit well
    # above the Latin cap height — so the two scripts need more air between them
    # than a second Latin line would, or the marks crowd the baseline above.
    name_leading = 34
    rule_gap, rule_h = 30, 5

    # Centre the whole block against the portrait rather than hanging it from the
    # top, so a short name and a long one both sit level with the face.
    block_h = name_font.size
    if ne_font:
        block_h += name_leading + ne_font.size
    block_h += rule_gap + rule_h
    if title_font:
        block_h += 26 + title_font.size
    y = photo_y + (PHOTO_SIZE - block_h) // 2

    draw.text((text_x, y), name_text, font=name_font, fill=WHITE)
    y += name_font.size

    if ne_font:
        y += name_leading
        draw.text((text_x, y), ne_text, font=ne_font, fill=MUTED)
        y += ne_font.size

    y += rule_gap
    draw.rectangle((text_x, y, text_x + 118, y + rule_h), fill=CRIMSON)
    y += rule_h

    if title_font:
        y += 26
        draw.text((text_x, y), title_text, font=title_font, fill=MUTED)

    # The bottom row carries the tagline against the domain, so the card closes
    # the same way the site banner does instead of trailing off.
    baseline = HEIGHT - 82
    draw.text((LEFT_EDGE, baseline), TAGLINE, font=_vesper(27, "Regular"), fill=MUTED)
    domain_font = _vesper(29, "Medium")
    draw.text(
        (RIGHT_EDGE - domain_font.getlength(DOMAIN), baseline),
        DOMAIN,
        font=domain_font,
        fill=MUTED,
    )

    buffer = BytesIO()
    # Progressive: a 1200px card is fetched over a phone connection by every
    # unfurler that shows a preview inline.
    card.convert("RGB").save(
        buffer, format="JPEG", quality=86, optimize=True, progressive=True
    )
    return buffer.getvalue()
