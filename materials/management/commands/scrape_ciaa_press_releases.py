"""Ingest CIAA press releases (प्रेस विज्ञप्ति) into Materials via the API plane.

Replaces the retired ``ciaa_press_releases`` Scrapy spider (archived
``Jawafdehi/ngm``). The old pipeline was three hops — spider → R2 files + the
``ngm_v1`` DocumentSource index → ``sync_materials_from_index`` → Materials — but
``ngm_v1`` is now FROZEN, so no NEW press release can reach the archive. This
command closes that gap: it walks CIAA's sequential press-release ids, shapes each
into Material JSON-LD (``materials.sourcing.ciaa``), and writes it through the
material REST plane — ``PUT /api/materials/<source>/<ident>`` for the doc, then
``POST …/file`` for each attachment. It is a thin CLIENT; it never touches the ORM.

Idempotency + resume, without a checkpoint file: before fetching an id it asks the
material API whether ``/material/ciaa_press_release/<id>`` already exists (a public
GET) and SKIPS it if so. The ~thousands of historical press releases already exist
as Materials (from the frozen-index sync), so they are skipped cheaply and their
durable R2 links are preserved; only genuinely-new ids are fetched and ingested.
Gaps self-heal (a missing id is re-fetched). The crawl stops after
``--max-consecutive-missing`` real 302/404s from CIAA (the end of the id range).

Dry-run by default (fetch + parse + report, write nothing); ``--write`` posts. The
material write endpoints are NGM-role gated, so ``--write`` needs a Caseworker
bearer (base URL via ``MATERIAL_API_BASE`` / ``INGESTION_API_BASE``). The bearer is
resolved by ``review.oidc_client_credentials.resolve_service_bearer``: a static
``INGESTION_API_TOKEN`` (local dev), else an OIDC client-credentials grant as the
``sa-ingestion`` account (``INGESTION_OIDC_CLIENT_ID/SECRET``) or the shared
``CASEWORK_OIDC_*`` service account — so the CronJob carries no static token. The
existence GET used for resume is public, so a dry run needs no bearer.

    manage.py scrape_ciaa_press_releases --start-id 3400            # dry-run recon
    INGESTION_API_TOKEN=… manage.py scrape_ciaa_press_releases --write   # the CronJob run
    manage.py scrape_ciaa_press_releases --start-id 3000 --limit 5  # bounded smoke test

The pure parse/shape halves live in ``materials.sourcing.ciaa`` (unit-tested).
"""

from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from materials.sourcing.ciaa.parse import parse_press_release
from materials.sourcing.ciaa.shaper import CIAA_PRESS_SOURCE, press_release_to_jsonld

#: CIAA press-release page base (``…/pressrelease/<id>``) and the one-shot language
#: toggle that pins the session to Nepali for Devanagari titles.
PRESS_RELEASE_BASE = "https://ciaa.gov.np/pressrelease/"
CHANGE_LANG_URL = "https://ciaa.gov.np/changeLang/1"

_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Jawafdehi-ciaa-crawler"

#: Default material-API base (local/dev). The CronJob overrides it with the
#: in-cluster platform service via ``MATERIAL_API_BASE`` / ``INGESTION_API_BASE``.
_DEFAULT_API_BASE = "http://127.0.0.1:8080"

#: HTTP statuses CIAA returns for a non-existent press release (302 → site root).
_MISSING_STATUSES = frozenset({301, 302, 404})


class CiaaSourceClient:
    """Fetches CIAA press-release pages + attachments over one session.

    Redirects are NOT followed on the page fetch so a missing id reads as its raw
    302 (following it would land on the 200 homepage and look like a hit). The
    language is pinned to Nepali once per session. Never raises — a transport
    failure returns ``status=None`` so the caller treats it as transient, not as
    the end of the range.
    """

    def __init__(self, timeout: int = 60):
        import requests

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})
        self._timeout = timeout
        self._lang_pinned = False

    def _ensure_nepali(self) -> None:
        if self._lang_pinned:
            return
        try:
            self._session.get(CHANGE_LANG_URL, timeout=self._timeout, allow_redirects=False)
        except Exception:  # noqa: BLE001 — best-effort; content is Devanagari regardless.
            pass
        self._lang_pinned = True

    def get_press_release(self, press_id: int) -> tuple[int | None, str]:
        self._ensure_nepali()
        try:
            resp = self._session.get(
                f"{PRESS_RELEASE_BASE}{press_id}", timeout=self._timeout, allow_redirects=False
            )
        except Exception:  # noqa: BLE001
            return None, ""
        return resp.status_code, resp.text

    def page_url(self, press_id: int) -> str:
        return f"{PRESS_RELEASE_BASE}{press_id}"

    def download(self, url: str) -> tuple[int | None, bytes, str]:
        try:
            resp = self._session.get(url, timeout=self._timeout, allow_redirects=True)
        except Exception:  # noqa: BLE001
            return None, b"", ""
        return resp.status_code, resp.content, resp.headers.get("Content-Type", "")


class MaterialApiClient:
    """Thin client for the material REST plane (existence GET + PUT doc + /file).

    ``token`` is optional: the existence GET is public (unauthenticated), so a
    dry-run recon against prod needs no credentials; ``PUT`` / ``/file`` are
    NGM-role gated and require the bearer.
    """

    def __init__(self, base_url: str, token: str | None, timeout: int = 60):
        import requests

        self._base = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        if token:
            self._session.headers.update({"Authorization": f"Bearer {token}"})
        self._timeout = timeout

    def _url(self, source: str, ident: str, suffix: str = "") -> str:
        return f"{self._base}/api/materials/{source}/{ident}{suffix}"

    def exists(self, source: str, ident: str) -> bool:
        """True iff a live material is stored at this ``@id``. A transport error
        returns False (can't confirm → don't skip; the upsert is idempotent so
        re-ingesting is safe, just wasteful)."""
        try:
            resp = self._session.get(self._url(source, ident), timeout=self._timeout)
        except Exception:  # noqa: BLE001
            return False
        return resp.status_code == 200

    def put_document(self, source: str, ident: str, doc: dict, material_type: str) -> tuple[int, object]:
        resp = self._session.put(
            self._url(source, ident),
            json={"material": doc, "material_type": material_type},
            timeout=self._timeout,
        )
        return resp.status_code, _safe_json(resp)

    def upload_file(
        self,
        source: str,
        ident: str,
        *,
        filename: str,
        content: bytes,
        content_type: str,
        role: str,
        source_url: str,
        skip_convert: bool,
        material_type: str,
    ) -> tuple[int, object]:
        files = {"file": (filename, content, content_type or "application/octet-stream")}
        data = {
            "role": role,
            "source_url": source_url or "",
            "material_type": material_type,
        }
        if skip_convert:
            data["skip_convert"] = "true"
        resp = self._session.post(
            self._url(source, ident, "/file"), files=files, data=data, timeout=self._timeout
        )
        return resp.status_code, _safe_json(resp)


def _safe_json(resp) -> object:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return (resp.text or "")[:200]


def build_source_client(timeout: int) -> CiaaSourceClient:
    """Factory (a seam for tests to inject a fake source transport)."""
    return CiaaSourceClient(timeout=timeout)


def build_material_client(base_url: str, token: str | None, timeout: int) -> MaterialApiClient:
    """Factory (a seam for tests to inject a fake material client)."""
    return MaterialApiClient(base_url, token, timeout=timeout)


def attachment_roles(file_urls: list[str]) -> list[tuple[str, str]]:
    """Assign an upload role to each attachment: the (first) PDF is RAW, the rest
    ALTERNATE — matching the legacy index's promotion. With no PDF, the first file
    is RAW."""
    if not file_urls:
        return []
    raw_index = next((i for i, url in enumerate(file_urls) if url.lower().endswith(".pdf")), 0)
    return [(url, "RAW" if i == raw_index else "ALTERNATE") for i, url in enumerate(file_urls)]


def _filename_for(url: str, press_id: int) -> str:
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].strip()
    return name or f"{press_id}"


class Command(BaseCommand):
    help = "Ingest CIAA press releases into Materials via the material REST plane."

    def add_arguments(self, parser):
        parser.add_argument(
            "--start-id", type=int, default=1,
            help="first press-release id to scan (default 1)",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="max NEW press releases to ingest this run (0 = no limit)",
        )
        parser.add_argument(
            "--max-consecutive-missing", type=int, default=10,
            help="stop after this many consecutive 302/404s from CIAA (default 10)",
        )
        parser.add_argument(
            "--max-consecutive-transient", type=int, default=25,
            help="give up after this many consecutive transient failures (timeout / "
            "5xx / non-result) so a source outage can't loop forever (default 25)",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="re-ingest ids that already exist as materials (skip the existence check)",
        )
        parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout (s)")
        parser.add_argument(
            "--api-base", default=None,
            help="material API base URL (default: $MATERIAL_API_BASE / $INGESTION_API_BASE / loopback)",
        )
        parser.add_argument(
            "--api-token", default=None,
            help="bearer token for writes (default: $INGESTION_API_TOKEN)",
        )
        parser.add_argument(
            "--write", action="store_true",
            help="POST to the material API (default: dry-run — fetch + parse only)",
        )

    def handle(self, *args, **o):
        write = o["write"]
        source = build_source_client(o["timeout"])
        material = self._material_client(o, require_token=write)

        mode = "WRITE" if write else "DRY-RUN"
        self.stdout.write(
            f"scrape_ciaa_press_releases [{mode}] start-id={o['start_id']} "
            f"stop-after={o['max_consecutive_missing']} missing"
        )

        stats = {
            "scanned": 0, "skipped": 0, "fetched": 0, "missing": 0,
            "transient": 0, "put": 0, "files": 0, "failed": 0,
        }
        consecutive_missing = 0
        consecutive_transient = 0
        press_id = o["start_id"]

        while consecutive_missing < o["max_consecutive_missing"]:
            if consecutive_transient >= o["max_consecutive_transient"]:
                # A persistent outage (every fetch times out / 5xx) never produces a
                # 302 to trip the missing-stop, so bound it here rather than loop the
                # id space forever.
                self.stderr.write(
                    f"stop: {consecutive_transient} consecutive transient failures "
                    f"at id ~{press_id} (source likely down)"
                )
                break
            if o["limit"] and stats["fetched"] >= o["limit"]:
                self.stdout.write(f"stop: reached --limit {o['limit']} new ingested")
                break
            stats["scanned"] += 1

            if not o["force"] and material.exists(CIAA_PRESS_SOURCE, str(press_id)):
                stats["skipped"] += 1
                consecutive_missing = 0
                consecutive_transient = 0
                press_id += 1
                continue

            status, html = source.get_press_release(press_id)
            if status in _MISSING_STATUSES:
                stats["missing"] += 1
                consecutive_missing += 1
                consecutive_transient = 0  # a definitive 302/404 is progress
                press_id += 1
                continue
            if status != 200 or not html:
                # Transient (timeout / 5xx): don't count toward the end-of-range
                # stop, but DO bound it (above) so an outage can't loop forever.
                stats["transient"] += 1
                consecutive_transient += 1
                self.stderr.write(f"  id {press_id}: HTTP {status} (transient, skipped)")
                press_id += 1
                continue

            consecutive_missing = 0
            consecutive_transient = 0
            stats["fetched"] += 1
            record = parse_press_release(html, press_id=press_id, source_url=source.page_url(press_id))
            self.stdout.write(
                f"  id {press_id}: {record.title[:60]!r} "
                f"({len(record.file_urls)} file(s), date {record.publication_date_bs or '—'})"
            )
            if write:
                self._ingest(material, source, record, stats)
            press_id += 1

        self.stdout.write(
            f"done [{mode}]: " + " ".join(f"{key}={value}" for key, value in stats.items())
        )

    def _ingest(self, material, source, record, stats) -> None:
        """PUT the doc, then upload each attachment. One failure is logged and
        counted, never fatal to the run."""
        doc, material_type = press_release_to_jsonld(record)
        try:
            status, body = material.put_document(
                CIAA_PRESS_SOURCE, str(record.press_id), doc, material_type
            )
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            self.stderr.write(f"  id {record.press_id}: PUT raised: {exc}")
            return
        if status not in (200, 201):
            stats["failed"] += 1
            self.stderr.write(f"  id {record.press_id}: PUT {status}: {body}")
            return
        stats["put"] += 1

        # We already scraped the body text, so suppress the server-side re-OCR —
        # UNLESS there is no text (an image-only release), where OCR is worth it.
        skip_convert = bool((record.full_text or "").strip())
        for url, role in attachment_roles(record.file_urls):
            dstatus, content, ctype = source.download(url)
            if dstatus != 200 or not content:
                self.stderr.write(f"  id {record.press_id}: attachment {dstatus} {url}")
                continue
            try:
                ustatus, ubody = material.upload_file(
                    CIAA_PRESS_SOURCE, str(record.press_id),
                    filename=_filename_for(url, record.press_id),
                    content=content, content_type=ctype, role=role,
                    source_url=record.source_url, skip_convert=skip_convert,
                    material_type=material_type,
                )
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"  id {record.press_id}: /file raised: {exc}")
                continue
            if ustatus in (200, 201):
                stats["files"] += 1
            else:
                self.stderr.write(f"  id {record.press_id}: /file {ustatus}: {ubody}")

    def _material_client(self, o, *, require_token: bool) -> MaterialApiClient:
        base = (
            o["api_base"]
            or os.environ.get("MATERIAL_API_BASE")
            or os.environ.get("INGESTION_API_BASE")
            or _DEFAULT_API_BASE
        )
        if require_token:
            # Lazy import (keeps the OIDC dep off dry-run / --help).
            from review.oidc_client_credentials import OIDCTokenError, resolve_service_bearer

            try:
                token = resolve_service_bearer(o["api_token"])
            except OIDCTokenError as exc:
                raise CommandError(
                    f"--write bearer: OIDC client-credentials grant failed: {exc}"
                ) from exc
            if not token:
                raise CommandError(
                    "--write needs a bearer: set INGESTION_API_TOKEN, or the OIDC "
                    "client-credentials env (INGESTION_OIDC_CLIENT_ID/SECRET, else the "
                    "CASEWORK_OIDC_* service account), or pass --api-token."
                )
        else:
            # Dry-run: the existence GET is public, so no token is needed and we
            # never mint one (no reason to hit Zitadel for a read-only run). Use a
            # static/explicit token only if one happens to be set.
            token = o["api_token"] or os.environ.get("INGESTION_API_TOKEN")
        return build_material_client(base, token, o["timeout"])
