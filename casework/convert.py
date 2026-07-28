"""RAW -> MARKDOWN conversion stage. LOCAL WRITES ONLY.

This is the stage every other enricher waits on: `casework.common.pipeline`
reports `"no MARKDOWN role on press_release (N bound, all unconverted)"` as an
unmet prerequisite, and this module is what clears it.

Conversion engine: the in-repo `markitdown` + `likhit` pair that
`review/converter.py` and `materials/job_handlers.py` already use (declared in
`pyproject.toml` under the `bigo-enrichment` extra -- `uv sync --extra
bigo-enrichment`). `likhit` is Jawafdehi's MarkItDown plugin for Nepali PDFs and
legacy `.doc` files, which is exactly what the CIAA press releases (`.pdf` RAW +
`.doc` ALTERNATE) and Special Court orders (`.doc` RAW) are. Deliberately NOT
the `mcp__jawafdehi__convert_to_markdown` MCP tool: that tool wraps the SAME
MarkItDown+likhit stack, so it buys no conversion quality, and routing several
hundred materials through per-call tool round-trips would be far slower than one
in-process `MarkItDown` instance with the plugin loaded once.

Writes go to the local file endpoint (`POST /api/materials/<source>/<ident>/file`,
multipart, `role=MARKDOWN`), never to production. That endpoint stores the
markdown through `django.core.files.storage` and appends a roled MediaObject to
the material's JSON-LD, so the resulting MARKDOWN link is a real, fetchable URL
that `materials.source_text` can GET -- which a hand-written `PUT` of a made-up
`contentUrl` would not be.

Usage:
    uv run python -m casework.convert --dry-run
    uv run python -m casework.convert --slug case-0123 --apply
"""
import argparse
import json
import logging
import mimetypes
import os
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from casework.common.api import CaseworkApi
from casework.common.cli import (
    add_common_args,
    basic_auth_from_env,
    configure_run_logging,
    log_event,
    log_run_footer,
    log_run_header,
    print_summary,
    resolve_api_token,
    setup_logging,
)
from casework.common.materials import markdown_link, raw_links
from casework.common.pipeline import COURT_TYPES, PRESS_TYPES, RunReport

log = logging.getLogger("casework.convert")

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
#: The two material types the gate measures. `charge_sheet` is already at 100%
#: MARKDOWN coverage and is left out of the default target set.
DEFAULT_TYPES = tuple(dict.fromkeys(PRESS_TYPES + COURT_TYPES))
UNMET_NO_LINK = "no convertible link (RAW/ALTERNATE/SOURCE_PAGE) on the material"

_MD = None


def _markitdown():
    """Lazy singleton -- loading the likhit plugin is expensive."""
    global _MD
    if _MD is None:
        from markitdown import MarkItDown

        _MD = MarkItDown(enable_plugins=True)
    return _MD


def _encode_url(link):
    """Percent-encode the path so Devanagari/space-bearing filenames fetch.

    The CIAA press-release artefacts live at paths like
    `.../2572. जिल्ला कास्की, ... - 2.pdf`; passing that to urlopen raw raises
    `UnicodeEncodeError` before a request is ever made.
    """
    parts = urllib.parse.urlsplit(link)
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, urllib.parse.quote(parts.path),
        parts.query, parts.fragment))


def extract_markdown(link, timeout=180):
    """Download one artefact and convert it to Markdown. GET only."""
    suffix = Path(urllib.parse.urlsplit(link).path).suffix or ".pdf"
    req = urllib.request.Request(_encode_url(link), headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        blob = r.read()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        fh.write(blob)
        path = fh.name
    try:
        return _markitdown().convert(path).text_content or ""
    finally:
        os.unlink(path)


def convert_material(material, *, writer):
    """Return 'already' | 'converted' | 'failed'. Idempotent.

    'already' short-circuits before any download: re-extracting a material that
    is already converted would pay the full download+OCR cost to produce a
    result that is then discarded.

    Every convertible link is tried in order, not just the first: press releases
    carry RAW(.pdf) + ALTERNATE(.doc) for the same document, and a pdf with a
    dud text layer should fall through to the .doc rather than strand the
    material.
    """
    if markdown_link(material):
        return "already"
    links = raw_links(material)
    if not links:
        return "failed"
    for link in links:
        try:
            text = extract_markdown(link)
        except Exception as exc:  # noqa: BLE001 - one bad artefact != a dead material
            log.warning("extract failed for %s: %s", link, exc)
            continue
        if text and text.strip():
            writer(material, text)
            return "converted"
        log.info("empty extraction from %s", link)
    return "failed"


def iri_to_source_ident(iri):
    """Split a material IRI into its (source, ident) path components.

    `<base>/material/<source>/<ident>` where `source` may be multi-segment
    (`ciaa/press_releases`) and `ident` is the final segment.
    """
    path = urllib.parse.urlsplit(iri or "").path
    marker = "/material/"
    if marker not in path:
        raise ValueError(f"not a material IRI: {iri!r}")
    rest = path.split(marker, 1)[1].strip("/")
    source, sep, ident = rest.rpartition("/")
    if not sep or not source or not ident:
        raise ValueError(f"not a material IRI: {iri!r}")
    return source, ident


def _multipart(fields, files):
    """Build a multipart/form-data body. Returns (content_type, body bytes)."""
    boundary = uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += str(value).encode("utf-8") + b"\r\n"
    for name, (filename, blob) in files.items():
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        out += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'
        ).encode()
        out += blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", bytes(out)


def upload_markdown(api, material_iri, text, timeout=120):
    """POST the extracted markdown as a MARKDOWN-role file. Returns its link.

    Refuses any non-loopback base URL. `CaseworkApi` already blocks Basic auth
    off-loopback, but this stage is the one that writes hundreds of materials in
    a single unattended run, so the guard is repeated at the write itself.
    """
    host = urllib.parse.urlsplit(api.base_url).hostname
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError(
            f"convert writes to loopback ONLY; refusing to upload to {api.base_url!r}")
    source, ident = iri_to_source_ident(material_iri)
    content_type, body = _multipart(
        # skip_convert: the server would otherwise enqueue its own re-OCR of
        # what we just extracted. MARKDOWN is not in the server's convertible
        # role set today, but the flag makes the intent explicit and survives
        # that set widening.
        {"role": "MARKDOWN", "skip_convert": "1", "material_type": "unknown"},
        {"file": (f"{ident}.md", text.encode("utf-8"))},
    )
    url = f"{api.base_url}/materials/{source}/{ident}/file"
    with api._request("POST", url, data=body,
                      headers=api._headers(content_type), timeout=timeout) as r:
        doc = json.loads(r.read().decode() or "{}")
    media = doc.get("associatedMedia") or []
    if isinstance(media, dict):
        media = [media]
    for mo in media:
        if isinstance(mo, dict) and mo.get("jawafdehi:linkRole") == "MARKDOWN":
            return mo.get("contentUrl")
    return None


def build_api(args):
    """Construct the client. Basic (local DEV_AUTH) unless a token is given.

    The Bearer token comes from `resolve_api_token` -- i.e. from
    $JAWAFDEHI_API_TOKEN, or from the discouraged `--api-token` flag (which
    warns) -- never straight off `args.api_token`, so a token is not required
    to appear in this process's argv where `ps -af` exposes it.
    """
    token = resolve_api_token(args)
    if token:
        return CaseworkApi(
            args.api_base_url, token=token,
            allow_remote_writes=args.allow_remote_writes,
        )
    return CaseworkApi(
        args.api_base_url,
        basic=basic_auth_from_env(),
        allow_remote_writes=args.allow_remote_writes,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_common_args(parser)
    parser.add_argument("--material-type", action="append", default=[],
                        help=f"defaults to {'/'.join(DEFAULT_TYPES)}")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    logger, run_id, paths = configure_run_logging("convert", verbose=args.verbose)
    start_time = time.monotonic()

    types = tuple(args.material_type) or DEFAULT_TYPES
    api = build_api(args)
    report = RunReport()

    slugs = args.slug or [c.get("slug") for c in api.iter_cases()]
    if args.limit:
        slugs = slugs[:args.limit]

    log_run_header(
        logger, stage="convert", base_url=args.api_base_url, dry_run=args.dry_run,
        provider=args.provider, model=args.model, n_selected=len(slugs),
        run_id=run_id, paths=paths,
    )

    seen = set()
    for slug in slugs:
        if not slug:
            continue
        log_event(logger, paths["events"], run_id=run_id, stage="convert", slug=slug,
                  step="start", status="start", detail="")
        try:
            case = api.get_case(slug)
        except Exception as exc:  # noqa: BLE001
            report.record(slug, "convert", "error", f"case fetch failed: {exc}")
            log_event(logger, paths["events"], run_id=run_id, stage="convert", slug=slug,
                      step="fetch", status="error", detail=f"case fetch failed: {exc}",
                      level=logging.ERROR)
            continue
        for entry in case.get("evidence") or []:
            iri = entry.get("material_iri")
            material = entry.get("material") or {}
            if not material or material.get("material_type") not in types:
                continue
            # Deduplicate by material IRI: one material is bound to several
            # cases, and converting it once per case would both waste the
            # download and double-count it in the summary.
            if iri in seen:
                continue
            seen.add(iri)
            if markdown_link(material):
                report.record(slug, "convert", "already", iri)
                log_event(logger, paths["events"], run_id=run_id, stage="convert", slug=slug,
                          step="convert", status="already", detail=iri)
                continue
            if not raw_links(material):
                report.record(slug, "convert", "unmet", UNMET_NO_LINK)
                log_event(logger, paths["events"], run_id=run_id, stage="convert", slug=slug,
                          step="convert", status="unmet", detail=UNMET_NO_LINK,
                          level=logging.WARNING)
                continue
            if args.dry_run:
                report.record(slug, "convert", "would-convert", iri)
                log_event(logger, paths["events"], run_id=run_id, stage="convert", slug=slug,
                          step="convert", status="would-convert", detail=iri)
                continue

            def writer(_material, text, _iri=iri):
                # upload_markdown returns the new MARKDOWN link, or None if
                # the server response carried no MARKDOWN-role media object.
                # Raise rather than swallow that: convert_material() only
                # ever checks whether extraction produced non-empty text, so
                # a discarded None here reported "converted" even when
                # nothing was actually persisted server-side -- a false
                # success with zero test coverage of this path.
                link = upload_markdown(api, _iri, text)
                if not link:
                    raise RuntimeError(
                        f"upload of {_iri} returned no MARKDOWN-role link "
                        "(server response had no MARKDOWN media object)")

            try:
                status = convert_material(material, writer=writer)
            except Exception as exc:  # noqa: BLE001
                report.record(slug, "convert", "error", f"{iri}: {exc}")
                log_event(logger, paths["events"], run_id=run_id, stage="convert", slug=slug,
                          step="convert", status="error", detail=f"{iri}: {exc}",
                          level=logging.ERROR)
                continue
            reason = iri if status != "failed" else f"extraction failed: {iri}"
            report.record(slug, "convert", status, reason)
            log_event(logger, paths["events"], run_id=run_id, stage="convert", slug=slug,
                      step="convert", status=status, detail=reason,
                      level=logging.ERROR if status == "failed" else logging.INFO)

    stats = report.summary()
    stats["materials_seen"] = len(seen)
    print_summary(stats, args.dry_run, "convert (RAW -> MARKDOWN)")
    # Unmet is NOT a skip. A run that converted nothing because every material
    # was unconvertible must not print the same summary as a run with nothing
    # left to do, so the reasons are shown, not just counted.
    unmet = report.unmet_reasons()
    if unmet:
        print("  unmet reasons:")
        for reason, count in unmet.most_common():
            print(f"    {count} x {reason}")

    log_run_footer(
        logger, stage="convert", stats=stats,
        duration_s=time.monotonic() - start_time,
    )

    return report


if __name__ == "__main__":
    main()
