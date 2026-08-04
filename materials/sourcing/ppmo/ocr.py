"""Transcript enrichment for PPMO publications — the deferred OCR pass.

``materials.sourcing.ppmo.crawl`` ingests each publication's PDF as an
``OFFICIAL_REPORT`` material *without* a transcript, because PPMO's procurement
bulletins and annual reports are a mix of scanned images and legacy-font PDFs.
This module produces the transcript and PATCHes it onto the material's ``text``
field, making the contract-award tables inside those documents full-text
searchable.

**Two stages, cheapest first.** Measured over the real 108-document corpus
(1,239 pages):

1. **Free — no LLM, no credentials.** ~678 pages carry a recoverable text layer.
   Either it is already Unicode Devanagari, or it is *legacy-font* Nepali
   (``Chandra`` / ``Hisab`` / ``Preeti`` / ``Kantipur``) that extracts as Latin
   mojibake — ``;fj{hlgs vl/b klqsf`` — and converts losslessly to
   ``सार्वजनिक खरिद पत्रिका`` with ``npttf2utf``. This stage alone covers the
   2081/2082 bulletins and the 2082 annual report.
2. **Paid — Claude Opus 5 vision OCR via Bedrock.** ~561 pages are true scans
   with no text layer. Rendered at ``--dpi`` (default 150, matching
   ``review.converter``'s Bedrock payload ceiling) and transcribed page by page.
   Measured cost: **~$0.049/page** (in ≈2,021 tok, out ≈1,560 tok at
   $5/$25 per MTok) — about **$28 for the scanned remainder**.

Every page is checkpointed, so an interrupted run never re-bills a page that
already succeeded. ``--dry-run`` reports the free/paid split and the projected
spend without calling Bedrock.

    # free stage only — no AWS calls, no cost:
    python -m materials.sourcing.ppmo.ocr --cache /tmp/ppmo.jsonl --free-only \\
        --out /tmp/ppmo_transcripts.jsonl

    # cost projection, no spend:
    python -m materials.sourcing.ppmo.ocr --cache /tmp/ppmo.jsonl --dry-run

    # full run, publishing transcripts back to the material API:
    python -m materials.sourcing.ppmo.ocr --cache /tmp/ppmo.jsonl \\
        --out /tmp/ppmo_transcripts.jsonl \\
        --api-base http://127.0.0.1:8000 --basic-auth ocr:ocrpass
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .shaper import PPMO_SOURCE

#: Bedrock inference-profile id for Claude Opus 5 (verified ACTIVE in us-west-2).
DEFAULT_MODEL = "global.anthropic.claude-opus-5"
DEFAULT_REGION = "us-west-2"

#: Render DPI for scanned pages. 150 keeps the base64 PNG under Bedrock's
#: payload ceiling while still OCRing Devanagari cleanly — the same reasoning
#: (and default) as ``review.converter._patch_likhit_ocr_dpi``.
DEFAULT_DPI = 150

#: Per-image base64 ceiling Bedrock enforces on an Anthropic vision payload
#: (~5 MB), minus headroom for the JSON envelope. 150 DPI usually lands well
#: under it, but a dense full-colour scan can blow past it — one 2082 notice page
#: rendered to 5,689,680 B of base64 and came back as a ``ValidationException``.
#: :func:`render_page_png` downscales instead of dropping the page.
BEDROCK_MAX_B64_BYTES = 4_500_000

#: Read timeout for a single Bedrock vision call. boto3 defaults to 60 s, which
#: is *below* the observed Opus 5 latency on a dense Devanagari scan (46-108 s
#: measured), so the default silently killed pages that were still working.
DEFAULT_READ_TIMEOUT = 300

#: Measured per-page token usage on real PPMO scans (pilot, 2026-08). Used only
#: for the --dry-run projection; actual spend is reported from live usage.
MEASURED_IN_TOK = 2021
MEASURED_OUT_TOK = 1560
OPUS5_IN_PER_MTOK = 5.00
OPUS5_OUT_PER_MTOK = 25.00

#: Legacy Nepali TTF font names seen in the PPMO corpus. Chandra and Hisab are
#: not in npttf2utf's bundled map, but they use the Preeti-family keyboard
#: layout, so the PCS NEPALI mapping converts them correctly (verified against
#: the 2082 bulletin: ";fj{hlgs vl/b klqsf 83" → "सार्वजनिक खरिद पत्रिका ८३").
LEGACY_FONTS = frozenset(
    {
        "Preeti",
        "Kantipur",
        "Sagarmatha",
        "Chandra",
        "Hisab",
        "PCSNEPALI",
        "PCS NEPALI",
        "FONTASY_HIMALI_TT",
    }
)
#: Map name to convert legacy spans through (see LEGACY_FONTS).
LEGACY_MAP_AS = "PCS NEPALI"

_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")

#: A page needs paid OCR when its recoverable text layer is thinner than this
#: (chars). Below it, the page is a scan (or a near-empty divider) and only
#: vision OCR can read it.
TEXT_LAYER_MIN_CHARS = 200

#: Minimum fraction of "plausible" characters for an extracted text layer to be
#: accepted (see :func:`is_readable`). Length alone is NOT a sufficient test: some
#: PPMO scans carry a junk OCR layer in a font that reports itself as plain
#: "Helvetica" and extracts as dense mojibake (``yq[q{.* de[|- ffi qrrqtf``) that
#: no npttf2utf map can rescue. Such a page cleared a pure length gate and wrote
#: garbage into the searchable ``text`` field, so we score readability instead.
READABLE_MIN_RATIO = 0.75

#: Characters that make text plausibly readable: Devanagari, ASCII letters and
#: digits, whitespace, and ordinary punctuation. Mojibake is dominated by the
#: bracket/brace/slash soup that falls outside this set.
_PLAUSIBLE_RE = re.compile(r"[ऀ-ॿA-Za-z0-9\s.,;:!?()\[\]/%&'\"–—-]")


def is_readable(text: str) -> bool:
    """True if ``text`` looks like real prose rather than legacy-font mojibake.

    Two signals, because the corpus contains both scripts:

    * If the text carries a meaningful share of Devanagari, accept it — a
      converted or natively-Unicode Nepali page.
    * Otherwise it should be predominantly plain Latin/ASCII (the bulletins carry
      long English passages). Mojibake fails this: its characters are mostly
      punctuation and bracket noise, so the plausible-character ratio collapses.
    """
    stripped = "".join(text.split())
    if len(stripped) < TEXT_LAYER_MIN_CHARS:
        return False
    deva = len(_DEVANAGARI_RE.findall(stripped))
    if deva / len(stripped) >= 0.20:
        return True  # substantial real Devanagari
    plausible = len(_PLAUSIBLE_RE.findall(stripped))
    if plausible / len(stripped) < READABLE_MIN_RATIO:
        return False
    # Latin-only text should also read like words, not consonant/symbol soup:
    # require a sane share of vowels among its ASCII letters.
    letters = [c for c in stripped if c.isascii() and c.isalpha()]
    if len(letters) >= 40:
        vowels = sum(1 for c in letters if c.lower() in "aeiou")
        if vowels / len(letters) < 0.20:
            return False
    return True


UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"

OCR_PROMPT = (
    "Transcribe ALL text from this scanned page of a Nepali government "
    "procurement document. Preserve tables as markdown tables — these often "
    "contain contract awards (firm names, contract amounts, procuring "
    "entities), so keep every column and figure exact. Output ONLY the "
    "transcribed text in its original language (Devanagari for Nepali, Latin "
    "for English). No commentary, no preamble."
)


# ── free stage: text layer + legacy-font conversion ──────────────────────────


def _font_mapper():
    """The npttf2utf FontMapper over its bundled map, or ``None`` if absent.

    ``npttf2utf`` ships with the optional ``bigo-enrichment`` extra (installed
    alongside likhit). Returning ``None`` degrades the free stage to
    Unicode-only extraction rather than crashing.
    """
    try:
        import npttf2utf
        from npttf2utf import FontMapper
    except ImportError:
        return None
    map_json = Path(npttf2utf.__file__).parent / "map.json"
    if not map_json.exists():
        return None
    try:
        return FontMapper(str(map_json))
    except Exception:  # noqa: BLE001 — a bad map must not kill the run.
        return None


def extract_page_text(page, mapper) -> str:
    """The page's recoverable text: Unicode spans as-is, legacy spans converted.

    Walks spans (not the whole page) because a single page mixes encodings — an
    English paragraph in Times New Roman beside a Nepali table in Chandra. A
    span already containing Devanagari is kept verbatim; a span in a known
    legacy font is run through :data:`LEGACY_MAP_AS`; anything else (plain
    Latin) is kept as-is.
    """
    out: list[str] = []
    try:
        blocks = page.get_text("dict")["blocks"]
    except Exception:  # noqa: BLE001
        return ""
    for block in blocks:
        for line in block.get("lines", []):
            parts: list[str] = []
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text.strip():
                    continue
                if _DEVANAGARI_RE.search(text):
                    parts.append(text)  # already Unicode Nepali
                elif span.get("font") in LEGACY_FONTS and mapper is not None:
                    try:
                        parts.append(
                            mapper.map_to_unicode(text, from_font=LEGACY_MAP_AS)
                        )
                    except Exception:  # noqa: BLE001 — keep the raw span.
                        parts.append(text)
                else:
                    parts.append(text)  # plain Latin (English) text
            if parts:
                out.append("".join(parts))
    return "\n".join(out)


#: Never render below this scale (≈14 DPI at 72pt base) — past it the page is
#: unreadable anyway, so we stop shrinking and let the oversized attempt go.
_MIN_RENDER_SCALE = 0.2


def render_page_png(page, dpi: int, max_b64: int = BEDROCK_MAX_B64_BYTES) -> bytes:
    """Render ``page`` to PNG, downscaling until Bedrock will accept the payload.

    A page whose base64 exceeds :data:`BEDROCK_MAX_B64_BYTES` is rejected outright
    with a ``ValidationException``, costing the page entirely. Losing resolution
    beats a hole in the transcript, so we shrink instead.

    The next scale is *solved for* rather than stepped: encoded size tracks pixel
    count, which is quadratic in scale, so ``scale * sqrt(limit/actual)`` lands
    near the ceiling in one move. Fixed decrements are not guaranteed to converge
    — a dense page can survive several and still be too big. The 0.9 factor is
    headroom against PNG's imperfect quadratic fit. Returns the last render if
    even :data:`_MIN_RENDER_SCALE` is too big, so the caller always gets bytes.
    """
    import pymupdf

    scale = dpi / 72
    png = b""
    for _ in range(6):
        png = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")
        # 4 base64 chars per 3 bytes; compare without materializing the encoding.
        b64_len = -(-len(png) // 3) * 4
        if b64_len <= max_b64:
            return png
        if scale <= _MIN_RENDER_SCALE:
            break
        scale = max(_MIN_RENDER_SCALE, scale * (max_b64 / b64_len) ** 0.5 * 0.9)
    return png


# ── paid stage: Opus 5 vision OCR via Bedrock ────────────────────────────────


class BedrockOcr:
    """Transcribes a rendered page image with Claude Opus 5 on Bedrock."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        region: str = DEFAULT_REGION,
        read_timeout: int = DEFAULT_READ_TIMEOUT,
    ):
        import boto3
        from botocore.config import Config

        # boto3's 60s default read timeout is shorter than Opus 5 vision latency
        # on a dense scan, so it must be raised explicitly (see the constant).
        # retries are handled by the caller's own backoff loop, not botocore's.
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(
                read_timeout=read_timeout,
                connect_timeout=30,
                retries={"max_attempts": 0},
            ),
        )
        self._model = model
        self.in_tokens = 0
        self.out_tokens = 0
        # transcribe() runs on a thread pool; guard the counters.
        self._lock = threading.Lock()

    def transcribe(self, png: bytes, max_tokens: int = 8000) -> str:
        """Return the page transcript, or ``""`` on a non-fatal failure.

        Opus 5 runs adaptive thinking by default, so ``content`` can lead with a
        ``thinking`` block — concatenate only the ``text`` blocks rather than
        indexing ``content[0]``, which would raise or return reasoning.
        """
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(png).decode(),
                            },
                        },
                        {"type": "text", "text": OCR_PROMPT},
                    ],
                }
            ],
        }
        resp = self._client.invoke_model(modelId=self._model, body=json.dumps(body))
        payload = json.loads(resp["body"].read())
        usage = payload.get("usage") or {}
        with self._lock:
            self.in_tokens += int(usage.get("input_tokens") or 0)
            self.out_tokens += int(usage.get("output_tokens") or 0)
        return "".join(
            b.get("text", "")
            for b in payload.get("content", [])
            if b.get("type") == "text"
        )

    @property
    def cost_usd(self) -> float:
        """Spend so far, from live usage (not the projection constants)."""
        return (
            self.in_tokens / 1e6 * OPUS5_IN_PER_MTOK
            + self.out_tokens / 1e6 * OPUS5_OUT_PER_MTOK
        )


# ── material API client ──────────────────────────────────────────────────────


class MaterialPatchError(Exception):
    """A transcript PATCH returned a non-2xx (retryable)."""


class MaterialPatcher:
    """PATCHes a transcript onto an existing material's ``text`` field."""

    def __init__(
        self, api_base: str, token: str | None, basic_auth=None, timeout: int = 60
    ):
        import requests

        self._base = api_base.rstrip("/")
        self._s = requests.Session()
        self._s.headers.update({"Accept": "application/json"})
        if token:
            self._s.headers["Authorization"] = f"Bearer {token}"
        elif basic_auth:
            self._s.auth = basic_auth
        self._timeout = timeout

    def patch_text(self, content_id: int | str, transcript: str) -> None:
        """Store ``transcript`` as the material's language-tagged ``text``.

        Re-PUTs through the single-material API (idempotent upsert by ``@id``),
        preserving the doc and adding only ``text`` — the searchable transcript.
        """
        url = f"{self._base}/api/materials/{PPMO_SOURCE}/{content_id}"
        try:
            got = self._s.get(url, timeout=self._timeout)
            if got.status_code >= 300:
                raise MaterialPatchError(f"GET {got.status_code}: {got.text[:160]}")
            doc = got.json()
            doc["text"] = {"ne": transcript}
            put = self._s.put(
                url,
                json={"material": doc, "material_type": "official_report"},
                timeout=self._timeout,
            )
            if put.status_code >= 300:
                raise MaterialPatchError(f"PUT {put.status_code}: {put.text[:160]}")
        except MaterialPatchError:
            raise
        except Exception as e:  # noqa: BLE001
            raise MaterialPatchError(str(e)[:160]) from e


# ── orchestration ────────────────────────────────────────────────────────────


class Checkpoint:
    """Per-document transcript state, so a re-run never re-bills a done page."""

    def __init__(self, out_path: Path):
        self.out_path = out_path
        self.state_path = out_path.with_suffix(out_path.suffix + ".state.json")
        self.done: dict[str, dict[str, Any]] = {}
        if self.out_path.exists():
            with self.out_path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cid = str(rec.get("content_id") or "")
                    if cid:
                        self.done[cid] = rec
        self._fh = out_path.open("a", encoding="utf-8")

    def record(self, rec: dict[str, Any]) -> None:
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        self.done[str(rec["content_id"])] = rec

    def close(self) -> None:
        self._fh.close()


def _fetch_pdf(session, url: str) -> bytes | None:
    try:
        r = session.get(url, timeout=120)
        return r.content if r.ok else None
    except Exception:  # noqa: BLE001
        return None


def run(args) -> None:
    import pymupdf
    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    session.verify = False

    records = [
        json.loads(line)
        for line in Path(args.cache).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.ids:
        wanted = {int(i) for i in args.ids.split(",") if i.strip().isdigit()}
        records = [r for r in records if int(r["content_id"]) in wanted]

    mapper = _font_mapper()
    if mapper is None:
        print(
            "  ! npttf2utf unavailable — legacy-font pages will need paid OCR "
            "(install the bigo-enrichment extra to recover them for free).",
            file=sys.stderr,
        )

    cp = Checkpoint(Path(args.out)) if args.out else None
    ocr = None
    patcher = None
    if not args.dry_run and not args.free_only:
        ocr = BedrockOcr(
            model=args.model, region=args.region, read_timeout=args.read_timeout
        )
    if args.api_base and not args.dry_run:
        patcher = MaterialPatcher(
            args.api_base, args.token, getattr(args, "basic_auth", None)
        )

    free_pages = paid_pages = 0
    docs_done = docs_skipped = patched = 0
    try:
        for rec in records:
            cid = str(rec["content_id"])
            if cp and cid in cp.done and not args.force:
                docs_skipped += 1
                continue
            pdf = _fetch_pdf(session, rec["pdf_url"])
            if not pdf:
                print(f"  ! fetch failed cid={cid}", file=sys.stderr)
                continue
            try:
                doc = pymupdf.open(stream=pdf, filetype="pdf")
            except Exception:  # noqa: BLE001
                print(f"  ! unreadable pdf cid={cid}", file=sys.stderr)
                continue

            # Pass 1 (local, free): classify every page and keep the readable
            # text layer. Slots stay page-indexed so the transcript preserves
            # document order regardless of how the OCR pass completes.
            n_pages = doc.page_count
            slots: list[str] = [""] * n_pages
            todo: list[tuple[int, bytes]] = []
            doc_free = doc_paid = 0
            for page in doc:
                text = extract_page_text(page, mapper)
                # Readability, not length: a junk OCR layer can be long AND
                # unreadable (see is_readable), and accepting it would write
                # mojibake into the searchable text field.
                if is_readable(text):
                    slots[page.number] = text
                    doc_free += 1
                    continue
                # No usable text layer → this page needs vision OCR.
                doc_paid += 1
                if ocr is None:
                    continue  # --dry-run / --free-only: count it, don't spend.
                todo.append((page.number, render_page_png(page, args.dpi)))
            doc.close()

            # Pass 2 (paid): OCR the scan pages CONCURRENTLY. Sequential calls
            # ran ~30s/page, so a 130-page scan took over an hour on its own;
            # Bedrock handles the fan-out fine and the token cost is identical.
            if todo:

                def _ocr_page(item: tuple[int, bytes]) -> tuple[int, str]:
                    idx, png = item
                    for attempt in range(1, args.retries + 1):
                        try:
                            return idx, ocr.transcribe(png, max_tokens=args.max_tokens)
                        except Exception as e:  # noqa: BLE001 — throttle/transient
                            if attempt == args.retries:
                                print(
                                    f"  ! OCR gave up cid={cid} p{idx}: {str(e)[:110]}",
                                    file=sys.stderr,
                                )
                                return idx, ""
                            time.sleep(min(30.0, 2.0**attempt))
                    return idx, ""

                with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                    for idx, text in pool.map(_ocr_page, todo):
                        slots[idx] = text

            parts = slots
            free_pages += doc_free
            paid_pages += doc_paid
            transcript = "\n\n".join(p for p in parts if p.strip())

            if cp and not args.dry_run:
                cp.record(
                    {
                        "content_id": rec["content_id"],
                        "title": rec.get("title"),
                        "pages": n_pages,
                        "free_pages": doc_free,
                        "ocr_pages": doc_paid,
                        "chars": len(transcript),
                        "transcript": transcript,
                    }
                )
            if patcher and transcript:
                try:
                    patcher.patch_text(rec["content_id"], transcript)
                    patched += 1
                except MaterialPatchError as e:
                    print(f"  ! PATCH failed cid={cid}: {e}", file=sys.stderr)
            docs_done += 1
            spent = f" spent=${ocr.cost_usd:.2f}" if ocr else ""
            print(
                f"  cid={cid:>6} {n_pages:>4}p free={doc_free:<4} ocr={doc_paid:<4} "
                f"chars={len(transcript):<7}{spent}",
                file=sys.stderr,
            )
    finally:
        if cp:
            cp.close()

    print(
        f"\ndone: docs={docs_done} skipped={docs_skipped} patched={patched} | "
        f"free_pages={free_pages:,} ocr_pages={paid_pages:,}",
        file=sys.stderr,
    )
    if ocr:
        print(
            f"  Opus 5 usage: in={ocr.in_tokens:,} out={ocr.out_tokens:,} "
            f"→ ACTUAL SPEND ${ocr.cost_usd:.2f}",
            file=sys.stderr,
        )
    elif paid_pages:
        est = paid_pages * (
            MEASURED_IN_TOK / 1e6 * OPUS5_IN_PER_MTOK
            + MEASURED_OUT_TOK / 1e6 * OPUS5_OUT_PER_MTOK
        )
        print(
            f"  projection: {paid_pages:,} pages would need Opus 5 OCR "
            f"≈ ${est:.2f} (at measured {MEASURED_IN_TOK}/{MEASURED_OUT_TOK} tok/page)",
            file=sys.stderr,
        )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Transcribe PPMO publication PDFs (free text layer + Opus 5 OCR)."
    )
    ap.add_argument("--cache", required=True, help="ppmo.jsonl from the ppmo crawler")
    ap.add_argument("--out", help="transcripts.jsonl (resume checkpoint)")
    ap.add_argument("--ids", help="Only these content ids (comma-separated)")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the free/paid split + projected spend; no Bedrock calls",
    )
    ap.add_argument(
        "--free-only",
        action="store_true",
        help="Free stage only (text layer + npttf2utf); never call Bedrock",
    )
    ap.add_argument(
        "--force", action="store_true", help="Re-do documents already in the checkpoint"
    )
    ap.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Bedrock model id (default {DEFAULT_MODEL})",
    )
    ap.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help=f"AWS region (default {DEFAULT_REGION})",
    )
    ap.add_argument(
        "--dpi",
        type=int,
        default=DEFAULT_DPI,
        help=f"Render DPI for scans (default {DEFAULT_DPI})",
    )
    ap.add_argument(
        "--max-tokens",
        type=int,
        default=8000,
        help="Max output tokens per page (default 8000)",
    )
    ap.add_argument(
        "--retries", type=int, default=4, help="OCR attempts per page (default 4)"
    )
    ap.add_argument(
        "--read-timeout",
        type=int,
        default=DEFAULT_READ_TIMEOUT,
        help=f"Bedrock read timeout in seconds (default {DEFAULT_READ_TIMEOUT}; "
        "boto3's own 60s default is below Opus 5 vision latency on dense scans)",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Concurrent Bedrock OCR calls per document (default 8, matching the "
        "project's BEDROCK_MAX_WORKERS). Sequential OCR measured ~30s/page, so a "
        "130-page scan took over an hour on its own; token cost is unchanged.",
    )
    ap.add_argument(
        "--api-base", help="Platform base URL, to PATCH transcripts onto materials"
    )
    ap.add_argument("--token", help="Bearer for the material API")
    ap.add_argument(
        "--basic-auth",
        dest="basic_auth_raw",
        metavar="USER:PASS",
        help="HTTP Basic for a LOCAL DEV_AUTH server",
    )
    args = ap.parse_args(argv)
    args.basic_auth = None
    if args.basic_auth_raw:
        if ":" not in args.basic_auth_raw:
            ap.error("--basic-auth must be USER:PASS")
        user, _, password = args.basic_auth_raw.partition(":")
        args.basic_auth = (user, password)
    if not args.dry_run and not args.out:
        ap.error("--out is required unless --dry-run (it is the resume checkpoint).")
    run(args)


if __name__ == "__main__":
    main()
