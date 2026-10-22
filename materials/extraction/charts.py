"""Which pages carry a chart, and a page image for whoever reads one.

A bar or pie chart is drawn as vector art or pasted in as a raster image, and the
text tier recovers **none** of its numbers — they are not in the document's text
layer at all. Reading them means rendering the page and having a model read the
picture, which costs a model call per page and cannot run inside a request.

So this module does the half that is deterministic and cheap: it says *which*
pages carry a chart, turning "read this 446-page report" into a worklist that
averaged 46 pages per report across the CIAA corpus. Nothing here reads a value
off a chart, and nothing here writes a figure row.

The thresholds are inherited, not invented: they were calibrated in
``services/corruption-research/ciaa-annual-reports/_tools/find_figures.py``
against the FY2069/70 report, where ruled-table pages carry at most ~47 curve ops
(decorative header boxes) and real chart pages carry 248-3,778. The one change is
the caption signal, and it matters: that script read captions out of
``page.get_text()``, which on a legacy-font report is mojibake, so the caption
signal silently never fired on exactly the corpus it was written for. Here the
caption is matched against the *repaired* page text the text tier already
produced.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("materials.extraction")

#: A figure/chart caption, in Nepali or English. Anchored on a following numeral
#: because ``re`` has no word boundary that works across Devanagari: without it,
#: ``चित्र`` matches inside ordinary words like ``चित्रकार`` (painter) and
#: ``पार्श्वचित्र`` (profile), neither of which introduces any data.
#:
#: The numeral part is matched WHOLE (``चित्र २.१४``, not ``चित्र २``) so the
#: manifest can show which figure the page announces — that string is what a
#: reviewer checks a false positive against, and a truncated one points at a
#: whole chapter.
_CAPTION_PATTERN = re.compile(
    r"(?:चित्र|चार्ट|ग्राफ|रेखाचित्र|आरेख)\s*(?:नं\.?|न\.|सङ्ख्या|संख्या|:)?\s*"
    r"[\d०-९]+(?:[.।][\d०-९]+)*"
    r"|\b(?:Figure|Fig\.?|Chart|Graph)\s*[\dIVX]+(?:\.[\dIVX]+)*",
    re.IGNORECASE,
)

#: Bezier/quad curve operations on the page. Pie arcs and line charts are made of
#: them; a ruled table is made of straight lines.
_CURVE_MIN = 80

#: Distinctly coloured filled shapes — bars, pie slices. Black/white/grey fills
#: are table rules and headers, and are excluded by :func:`_is_colored`.
_COLOR_MIN = 4

#: Share of the page a single embedded image must cover to look like a chart
#: pasted in as a picture rather than a logo or a masthead.
_RASTER_COVER = 0.10

#: Render resolution for a page a model is going to read. 300 DPI is what the
#: CIAA extraction used and what its 97.95% cell-agreement figure was measured
#: at; dropping it makes printed data labels unreadable, which is the one thing
#: the render exists to preserve.
_RENDER_DPI = 300


def _is_colored(fill) -> bool:
    """True for a fill with real hue — not black, white or grey."""
    if fill is None or len(fill) != 3:
        return False
    r, g, b = fill
    if max(r, g, b) < 0.15:  # black-ish
        return False
    return max(abs(r - g), abs(g - b), abs(r - b)) > 0.12


def page_chart_signals(page, page_text: str = "") -> dict:
    """Why this page looks like it carries a chart. Empty dict = it does not.

    Returned rather than reduced to a boolean so the manifest can say *which*
    signal fired: "a caption says चित्र २.४" and "this page is 3,778 curve ops"
    are different kinds of evidence, and a reviewer triaging a false positive
    needs to know which one spoke.
    """
    signals: dict = {}

    captions = [m.group(0).strip() for m in _CAPTION_PATTERN.finditer(page_text or "")]
    if captions:
        signals["captions"] = captions[:8]

    page_area = abs(page.rect.width * page.rect.height) or 1.0
    cover = 0.0
    for image in page.get_images(full=True):
        for rect in page.get_image_rects(image[0]):
            cover = max(cover, abs(rect.width * rect.height) / page_area)
    if cover >= _RASTER_COVER:
        signals["raster_cover"] = round(cover, 3)

    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001 - a malformed content stream is not fatal
        drawings = []
    curves = 0
    colored = 0
    for drawing in drawings:
        if _is_colored(drawing.get("fill")):
            colored += 1
        for item in drawing.get("items", []):
            if item[0] in ("c", "qu"):
                curves += 1
    if curves >= _CURVE_MIN:
        signals["curves"] = curves
    if colored >= _COLOR_MIN:
        signals["colored_fills"] = colored

    return signals


def chart_page_signals(
    data: bytes, lines_by_page: dict[int, list[tuple[float, str]]] | None = None
) -> dict[int, dict]:
    """``{page_no: signals}`` for every page that looks like it carries a chart."""
    import fitz

    lines_by_page = lines_by_page or {}
    found: dict[int, dict] = {}
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        for index in range(doc.page_count):
            page_no = index + 1
            text = "\n".join(
                line for _y0, line in lines_by_page.get(page_no, []) or []
            )
            signals = page_chart_signals(doc[index], text)
            if signals:
                found[page_no] = signals
    finally:
        doc.close()
    return found


def detect_chart_pages(
    data: bytes, lines_by_page: dict[int, list[tuple[float, str]]] | None = None
) -> list[int]:
    """Just the page numbers, in order — the worklist the vision tier takes."""
    return sorted(chart_page_signals(data, lines_by_page))


def render_page_png(data: bytes, page_no: int, *, dpi: int = _RENDER_DPI) -> bytes:
    """One page as a PNG, for a model (or a human) to read the chart off."""
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        if not 1 <= page_no <= doc.page_count:
            raise ValueError(
                f"page {page_no} is outside this document's 1-{doc.page_count}"
            )
        return doc[page_no - 1].get_pixmap(dpi=dpi).tobytes("png")
    finally:
        doc.close()


def write_chart_renders(
    data: bytes,
    out_dir: Path,
    *,
    doc_id: str,
    pages: list[int],
    signals: dict[int, dict] | None = None,
    dpi: int = _RENDER_DPI,
) -> dict:
    """Render ``pages`` under ``out_dir`` and write a manifest beside them.

    The manifest is the handoff to the chart-reading pass: page number, the PNG,
    and the signals that nominated the page. It intentionally matches the shape
    ``_tools/find_figures.py`` writes, so the existing vision extraction can be
    pointed at this output without being rewritten first.

    Takes the page list rather than detecting again. Detection walks every page's
    drawing operators, which is a material share of the 160 s a 446-page report
    costs, and the caller has just paid it — re-running it here would also let
    the rendered set drift from the set the stored document reports.
    """
    signals = signals or {}
    pages_dir = out_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "doc_id": doc_id,
        "dpi": dpi,
        "candidates": [],
    }
    for page_no in sorted(pages):
        png = pages_dir / f"p{page_no:04d}.png"
        png.write_bytes(render_page_png(data, page_no, dpi=dpi))
        manifest["candidates"].append(
            {
                "page": page_no,
                "png": str(png.relative_to(out_dir)),
                "signals": signals.get(page_no, {}),
            }
        )
    (out_dir / "chart_pages.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("rendered %d chart page(s) for %s", len(manifest["candidates"]), doc_id)
    return manifest
