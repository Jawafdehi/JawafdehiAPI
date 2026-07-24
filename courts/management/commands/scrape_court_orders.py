"""Capture Supreme/Special court-order documents into the platform.

The recurring replacement for the retired ``supreme_court_orders`` Scrapy spider
(archived ``Jawafdehi/ngm``). Order documents live behind a CAPTCHA-gated search
form on ``supremecourt.gov.np/cp/`` — separate from the cause-list/detail pages
``scrape_courtcases`` walks. For each decided Supreme/Special case still missing
its order, this GETs the homepage (to seed the CAPTCHA session cookie), POSTs the
search form, parses the result page, downloads each order file to the durable
store, mints a ``court_order`` Material (``isPartOf`` the ngm case), and records a
canonical DocumentSource on the case's ``document_sources`` column.

Dry-run by default (GET→CAPTCHA→POST→parse, read-only, reports the outcome).
``--write`` persists (downloads, stores, materializes, updates the case).

    manage.py scrape_court_orders --limit 5                     # dry-run recon
    manage.py scrape_court_orders --case supreme:082-WO-0123    # one case, dry-run
    manage.py scrape_court_orders --court special --write       # persist backlog
    manage.py scrape_court_orders --write --limit 3000          # the CronJob run

The pure parse/shape/select halves live in ``courts.scraper.orders`` (unit-tested);
this command adds the live HTTP, the download→store→materialize plumbing, and the
``extra_data`` order-state writes.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import time
from datetime import date
from urllib.parse import urlparse

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max
from django.utils import timezone

from courts.models import CourtCase, CourtCaseHearing
from courts.scraper import orders as O
from jawafdehi_shared.storage import store_file_as_link
from materials.jsonld import MaterialType, court_order_to_jsonld
from materials.provenance import attach_media_object, build_provenance
from materials.single_source_ingest import upsert_single_source_material

_UA = "Mozilla/5.0 (X11; Linux x86_64) Jawafdehi-courts-crawler"

#: File-extension → encodingFormat for the order documents the portal serves.
_ENCODING = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument"
    ".wordprocessingml.document",
}

#: Homepage GETs tolerated when the CAPTCHA cookie fails to appear.
MAX_HOMEPAGE_RETRIES = 3
#: Fresh-session retries after the server rejects the CAPTCHA answer.
MAX_CAPTCHA_RETRIES = 5

#: Opt-in gate (mirrors the retired spider's ``ENABLE_CAPTCHA_COOKIE_EXTRACT``):
#: this command drives a CAPTCHA-gated search against a live government portal,
#: so it refuses to run — even a read-only dry-run — unless this env is truthy.
#: The recurring CronJob sets it; an accidental/unattended invocation stays inert.
CAPTURE_ENABLED_ENV = "ENABLE_COURT_ORDER_CAPTURE"


class OrdersHttpClient:
    """The live ``/cp`` transport: a fresh cookie jar per case (a returning
    ``court_session`` cookie suppresses the Set-Cookie the CAPTCHA answer rides
    on), read-only GET/POST/download. Never raises — a transport failure returns
    ``status=None`` so the caller can treat it as a transient, re-crawlable error.
    """

    def __init__(self, timeout: int = 60):
        import requests

        self._requests = requests
        self._timeout = timeout
        self._session = None

    def new_session(self) -> None:
        self._session = self._requests.Session()
        self._session.headers.update(
            {
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def homepage(self) -> tuple[int | None, str | None, str | None]:
        """GET the homepage → ``(status, court_session_cookie_value, retry_after)``."""
        try:
            resp = self._session.get(O.HOMEPAGE_URL, timeout=self._timeout)
        except Exception:
            return None, None, None
        return (
            resp.status_code,
            self._session.cookies.get("court_session"),
            resp.headers.get("Retry-After"),
        )

    def search(self, form: dict) -> tuple[int | None, str, str | None]:
        """POST the search form → ``(status, html, retry_after)``."""
        headers = {"Origin": O.ORIGIN, "Referer": O.HOMEPAGE_URL}
        try:
            resp = self._session.post(
                O.HOMEPAGE_URL, data=form, headers=headers, timeout=self._timeout
            )
        except Exception:
            return None, "", None
        return resp.status_code, resp.text, resp.headers.get("Retry-After")

    def download(self, url: str) -> tuple[int | None, bytes]:
        """GET one order file → ``(status, content)`` (uses the CAPTCHA session)."""
        try:
            resp = self._session.get(url, timeout=self._timeout)
        except Exception:
            return None, b""
        return resp.status_code, resp.content


def build_http_client(timeout: int) -> OrdersHttpClient:
    """Factory (a seam for tests to inject a fake transport)."""
    return OrdersHttpClient(timeout=timeout)


class _Backoff:
    """A polite server-error backoff — exponential, honoring ``Retry-After``.

    The June rate experiment found no per-IP wall at ~1,200 cases/hr off-peak but
    could not test Nepal midday peak; this self-regulates that untested window
    (REPORT.md rec #2). ``sleep`` is injected so tests don't wait.
    """

    LADDER = (30, 60, 120, 300, 600)

    def __init__(self, sleep):
        self._sleep = sleep
        self._idx = -1

    def ok(self) -> None:
        self._idx = -1

    def server_error(self, retry_after: str | None = None) -> None:
        self._idx = min(self._idx + 1, len(self.LADDER) - 1)
        secs = self.LADDER[self._idx]
        try:
            secs = max(secs, int(float(retry_after))) if retry_after else secs
        except (TypeError, ValueError):
            pass
        self._sleep(secs)


class Command(BaseCommand):
    help = "Capture Supreme/Special court-order documents into the platform."

    def add_arguments(self, parser):
        parser.add_argument(
            "--court", default="all", help="supreme | special | all (default all)"
        )
        parser.add_argument(
            "--limit", type=int, default=3000, help="max cases per run (default 3000)"
        )
        parser.add_argument(
            "--delay", type=float, default=3.0,
            help="seconds between cases (default 3.0; the proven-polite prod rate)",
        )
        parser.add_argument(
            "--case", default=None,
            help="a single 'court:case_number' (smoke test; ignores backlog filters)",
        )
        parser.add_argument(
            "--today", default=None, help="AD anchor date YYYY-MM-DD (default: today)"
        )
        parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout (s)")
        parser.add_argument(
            "--write", action="store_true",
            help="download + persist (default: dry-run — GET/POST/parse only)",
        )

    def handle(self, *args, **o):
        if os.environ.get(CAPTURE_ENABLED_ENV, "").strip().lower() not in ("1", "true", "yes"):
            raise CommandError(
                "court-order capture is DISABLED. This command drives a "
                "CAPTCHA-gated search against supremecourt.gov.np; set "
                f"{CAPTURE_ENABLED_ENV}=1 to run it (the recurring CronJob sets this)."
            )
        courts = self._resolve_courts(o["court"])
        today = self._anchor(o["today"])
        write = o["write"]
        delay = max(0.0, o["delay"])
        cases = self._select_cases(o["case"], courts, o["limit"], today)
        client = build_http_client(o["timeout"])
        backoff = _Backoff(time.sleep)

        mode = "WRITE" if write else "DRY-RUN"
        self.stdout.write(
            f"scrape_court_orders [{mode}] courts={list(courts)} "
            f"cases={len(cases)} today={today}"
        )

        tally = {k: 0 for k in ("docs", "no_record", "too_recent", "failed", "transient")}
        for i, case in enumerate(cases, 1):
            outcome, detail = self._process(case, client, today, write, backoff)
            tally[outcome] = tally.get(outcome, 0) + 1
            self.stdout.write(
                f"  [{i}/{len(cases)}] {case.court_id}:{case.case_number} "
                f"-> {outcome}{f' ({detail})' if detail else ''}"
            )
            if delay and i < len(cases):
                time.sleep(delay)

        self.stdout.write(
            "done: "
            + " ".join(f"{k}={v}" for k, v in tally.items())
        )

    # ── case selection ───────────────────────────────────────────────────────

    def _select_cases(self, one, courts, limit, today):
        if one:
            court, _, case_number = one.partition(":")
            if not case_number:
                raise CommandError("--case must be 'court:case_number'")
            case = (
                CourtCase.objects.using("ngm")
                .filter(court_id=court, case_number=case_number, is_deleted=False)
                .first()
            )
            if case is None:
                raise CommandError(f"case not found: {court}:{case_number}")
            return [case]
        return list(O.orders_backlog_queryset(courts=courts, today=today)[:limit])

    @staticmethod
    def _resolve_courts(value):
        value = (value or "all").lower()
        if value == "all":
            return O.ORDER_COURTS
        if value in O.ORDER_COURTS:
            return (value,)
        raise CommandError(f"--court must be one of {('all', *O.ORDER_COURTS)}, got {value!r}")

    # ── per-case capture cycle ───────────────────────────────────────────────

    def _process(self, case, client, today, write, backoff):
        """Run the GET→CAPTCHA→POST→parse cycle and apply the outcome.

        Returns ``(outcome, detail)`` where outcome is one of docs/no_record/
        too_recent/failed/transient.
        """
        result = self._search_with_retries(case, client, backoff)
        if result is None:
            return self._apply_transient(case, "captcha_unresolved", write)

        if result.status == O.SERVER_ERROR:
            return self._apply_transient(case, f"server_error: {result.detail}"[:200], write)
        if result.status == O.NOT_RESULTS_PAGE:
            return self._apply_transient(case, "not_results_page", write)
        if result.status == O.NO_RECORD:
            return self._apply_no_record(case, today, write)
        # result.status == DOCS
        return self._capture_docs(case, result.doc_urls, client, write, backoff)

    def _search_with_retries(self, case, client, backoff):
        """GET→CAPTCHA→POST, retrying a fresh session on a missing/invalid CAPTCHA.

        Returns an ``OrderResult`` (docs/no_record/server_error/not_results_page),
        or ``None`` when the CAPTCHA could never be resolved this run.
        """
        for _ in range(MAX_CAPTCHA_RETRIES):
            captcha = None
            for _ in range(MAX_HOMEPAGE_RETRIES):
                client.new_session()
                status, cookie, retry_after = client.homepage()
                if status is None or status >= 500:
                    backoff.server_error(retry_after)
                    continue
                captcha = O.extract_captcha(cookie)
                if captcha:
                    break
            if not captcha:
                continue

            form = O.order_form_data(
                case.court_id, case.case_number, case.registration_date_bs, captcha
            )
            status, html, retry_after = client.search(form)
            if status is None or status >= 500:
                backoff.server_error(retry_after)
                continue
            backoff.ok()
            result = O.parse_order_results(html)
            if result.status == O.INVALID_CAPTCHA:
                continue  # fresh session, new CAPTCHA
            return result
        return None

    def _capture_docs(self, case, doc_urls, client, write, backoff):
        """Download each order file, store durably, and materialize."""
        if not write:  # dry-run: report the found documents without downloading
            return "docs", f"{len(doc_urls)} file(s) [dry-run]"

        files = []
        for url in doc_urls:
            status, content = client.download(url)
            if status is None or status >= 500:
                backoff.server_error()
                return self._apply_transient(case, f"download {status} {url}"[:200], write)
            if status != 200 or not content:
                return self._apply_transient(case, f"download {status} {url}"[:200], write)
            ext = (posixpath.splitext(urlparse(url).path)[1] or ".doc").lower()
            files.append(
                {
                    "source_url": url,
                    "content": content,
                    "is_pdf": ext == ".pdf",
                    "ext": ext,
                    "sha": hashlib.sha256(content).hexdigest(),
                    "len": len(content),
                }
            )
        backoff.ok()

        stored = []
        safe_case = case.case_number.replace("/", "-")
        for n, f in enumerate(files, 1):
            # A unique per-file name: the durable store hashes the NAME (not the
            # bytes), so two same-extension files on one case would otherwise
            # collide. Mirrors the retired pipeline's ``{case}.{n}{ext}``.
            name = f"{case.court_id}-{safe_case}.{n}{f['ext']}"
            link = store_file_as_link(ContentFile(f["content"], name=name))["link"]
            stored.append({**f, "link": link})

        src = O.build_order_document_source(case.court_id, case.case_number, stored)
        self._materialize(case, src, stored)

        case.document_sources = [src]
        case.extra_data = O.mark_success(
            case.extra_data,
            links=[link["link"] for link in src["url"]],
            now_iso=self._now_iso(),
        )
        case.save(
            using="ngm",
            update_fields=["document_sources", "extra_data", "updated_at"],
        )
        return "docs", f"{len(stored)} file(s)"

    def _materialize(self, case, src, stored):
        """Mint/refresh the standalone ``court_order`` Material for the case.

        Built from the DocumentSource, then the ``associatedMedia`` is rebuilt so
        each file carries capture provenance (sha256). Feeding the durable link in
        via ``court_order_to_jsonld`` AND ``attach_media_object`` would emit a
        no-sha duplicate MediaObject, so the roled media are (re)attached here.
        """
        doc = court_order_to_jsonld(
            src, court_identifier=case.court_id, case_number=case.case_number, n=None
        )
        doc["associatedMedia"] = []
        by_link = {f["link"]: f for f in stored}
        for link in src["url"]:
            f = by_link[link["link"]]
            attach_media_object(
                doc,
                content_url=link["link"],
                role=link["role"],
                encoding_format=_ENCODING.get(f["ext"]),
                provenance=build_provenance(
                    sha256=f["sha"],
                    fetch_method="scrape",
                    source_url=f["source_url"],
                    content_length=f["len"],
                ),
            )
        upsert_single_source_material(doc, material_type=MaterialType.COURT_ORDER)

    # ── outcome application ──────────────────────────────────────────────────

    def _apply_no_record(self, case, today, write):
        """No document on the portal: a permanent failure for an old decided case,
        else a soft ``too_recent`` re-check."""
        expected = O.is_document_expected(self._last_hearing(case), today=today)
        outcome = "failed" if expected else "too_recent"
        if not write:
            return outcome, "no document [dry-run]"
        if expected:
            case.extra_data = O.mark_failed(
                case.extra_data, error="no_document_old_case", now_iso=self._now_iso()
            )
        else:
            case.extra_data = O.mark_too_recent(case.extra_data, now_iso=self._now_iso())
        case.save(using="ngm", update_fields=["extra_data", "updated_at"])
        return outcome, "no document"

    def _apply_transient(self, case, error, write):
        if not write:
            return "transient", f"{error} [dry-run]"
        case.extra_data = O.mark_transient(
            case.extra_data, error=error, now_iso=self._now_iso()
        )
        case.save(using="ngm", update_fields=["extra_data", "updated_at"])
        return "transient", error

    # ── helpers ──────────────────────────────────────────────────────────────

    def _last_hearing(self, case) -> date | None:
        return (
            CourtCaseHearing.objects.using("ngm")
            .filter(court_id=case.court_id, case_number=case.case_number)
            .aggregate(m=Max("hearing_date_ad"))["m"]
        )

    @staticmethod
    def _now_iso() -> str:
        return timezone.now().isoformat()

    @staticmethod
    def _anchor(value) -> date:
        if not value:
            return timezone.localdate()
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise CommandError(f"--today must be YYYY-MM-DD, got {value!r}") from exc
