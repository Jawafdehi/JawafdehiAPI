"""Court-order capture (Supreme + Special) — the pure parse/shape/select core.

Ports the retired ``supreme_court_orders`` Scrapy spider (archived ``Jawafdehi/ngm``)
into the monolith. Order documents live behind a CAPTCHA-gated search form on
``supremecourt.gov.np/cp/`` that is SEPARATE from the cause-list / case-detail
pages the rest of ``courts.scraper`` walks — those detail pages carry no order
links (empirically confirmed). Only the Supreme and Special courts publish order
documents on this portal (district/high probed 0/400), so capture is scoped to
``ORDER_COURTS``.

This module holds only the deterministic halves — the CAPTCHA-cookie parse, the
result-page parse, DocumentSource shaping, the backlog selection query, and the
``extra_data`` order-state machine — so they are unit-testable without a network.
The HTTP + storage + DB orchestration lives in the ``scrape_court_orders``
management command (the recurring CronJob entrypoint).

Output contract (unchanged from the historical corpus):
  * ``document_sources`` on the ``CourtCase`` gets ONE canonical DocumentSource:
    ``{"document_id", "source_type": "COURT_ORDER", "url": [{"link", "role"}]}`` —
    ``url`` is the roled-link LIST (RAW = PDF-preferred primary, rest ALTERNATE),
    the shape ``materials.jsonld.media_objects_from_document_sources`` reads.
  * each order file becomes a Material MediaObject on the standalone
    ``court_order`` Material (built by ``court_order_to_jsonld`` /
    ``_materialize_orders``), carrying its capture provenance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from re import compile as re_compile
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup
from django.db.models import Case, F, IntegerField, Q, QuerySet, TextField, Value, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast

from courts.models import CourtCase

#: Courts that publish order documents on the ``/cp`` portal.
ORDER_COURTS = ("supreme", "special")

#: ``(court_type, court_id)`` search-form params per court (from the retired
#: ``ngm.utils.court_mapping``; Supreme/Special take fixed numeric ids).
_COURT_FORM_PARAMS = {
    "supreme": ("S", "264"),
    "special": ("T", "116"),
}

HOMEPAGE_URL = "https://supremecourt.gov.np/cp/"
ORIGIN = "https://supremecourt.gov.np"

# ── extra_data order-state markers (mirror the retired pipeline) ──────────────
#: Presence marks a case as captured — the backlog query excludes non-null.
K_ORDERS = "court_orders"
K_SCRAPED_AT = "court_orders_scraped_at"
K_FAILED = "orders_failed"
K_ERROR = "orders_error"
K_FAILED_AT = "orders_failed_at"
K_TOO_RECENT = "orders_too_recent"
K_TOO_RECENT_AT = "orders_too_recent_checked_at"
K_TRANSIENT_ERROR = "orders_transient_error"
K_TRANSIENT_AT = "orders_transient_at"
K_TRANSIENT_RETRIES = "orders_transient_retries"

#: Days since the last hearing before an order document is expected on the site.
MIN_DAYS_FOR_DOCUMENT_AVAILABILITY = 365
#: How often a soft-skipped ("too recent") case is re-checked.
TOO_RECENT_RECHECK_DAYS = 30
#: Transient (download/network) failures tolerated before a permanent give-up.
MAX_TRANSIENT_RETRIES = 5

#: Page markers on the CAPTCHA search-result page.
VALID_RESULTS_MARKER = "फैसला / आदेश को पुर्ण पाठ"
NO_RECORD_MARKER = "रेकर्ड भेटिएन"
INVALID_CAPTCHA_MARKER = "Invalid CAPTCHA"

#: The PHP-serialised session cookie embeds the answer as
#: ``...s:12:"captcha_word";s:5:"hello";...``.
_CAPTCHA_RE = re_compile(r'"captcha_word";s:\d+:"([^"]+)"')


def court_form_params(court_identifier: str) -> tuple[str, str]:
    """``(court_type, court_id)`` for the ``/cp`` search form.

    Raises ``ValueError`` for a court that does not publish orders on this portal.
    """
    try:
        return _COURT_FORM_PARAMS[court_identifier]
    except KeyError:
        raise ValueError(
            f"{court_identifier!r} has no court-order form params "
            f"(only {', '.join(ORDER_COURTS)} publish orders)"
        ) from None


def extract_captcha(cookie_value: str | None) -> str | None:
    """Pull the CAPTCHA answer out of a raw ``court_session`` cookie value.

    ``cookie_value`` is the raw (percent-encoded) cookie string set on the
    homepage GET. Returns the answer, or ``None`` when absent/unparseable.
    """
    if not cookie_value:
        return None
    match = _CAPTCHA_RE.search(unquote(cookie_value))
    return match.group(1) if match else None


def order_form_data(
    court_identifier: str, case_number: str, registration_date_bs: str | None, captcha: str
) -> dict[str, str]:
    """The POST body for the ``/cp`` order search (mirrors the retired spider)."""
    court_type, court_id = court_form_params(court_identifier)
    return {
        "court_type": court_type,
        "court_id": court_id,
        "regno": case_number,
        "darta_date": registration_date_bs or "",
        "faisala_date": "",
        "captcha": captcha,
        "submit": "submit",
    }


# ── result-page parse ────────────────────────────────────────────────────────

#: Result-page classifications.
DOCS = "docs"
NO_RECORD = "no_record"
INVALID_CAPTCHA = "invalid_captcha"
SERVER_ERROR = "server_error"
NOT_RESULTS_PAGE = "not_results_page"


@dataclass
class OrderResult:
    """Outcome of one ``/cp`` search POST."""

    status: str
    doc_urls: list[str] = field(default_factory=list)
    detail: str = ""


def parse_order_results(html: str, *, base_url: str = HOMEPAGE_URL) -> OrderResult:
    """Classify a ``/cp`` search response and extract order document URLs.

    Mirrors the retired spider's ``_do_parse_results`` order of checks:
      1. ``bgcolor="#FF6600"`` error table → ``invalid_captcha`` (retry a fresh
         session) or ``server_error`` (leave re-crawlable).
      2. missing the ``फैसला / आदेश को पुर्ण पाठ`` marker → ``not_results_page``.
      3. ``रेकर्ड भेटिएन`` → ``no_record``.
      4. else collect ``cells[9] a.download_content`` hrefs → ``docs``; an empty
         collection is treated as ``no_record``.
    """
    html = html or ""
    soup = BeautifulSoup(html, "html.parser")

    error_table = soup.find("table", attrs={"bgcolor": "#FF6600"})
    if error_table is not None:
        text = error_table.get_text(strip=True)
        if INVALID_CAPTCHA_MARKER in text:
            return OrderResult(INVALID_CAPTCHA, detail=text)
        return OrderResult(SERVER_ERROR, detail=text)

    if VALID_RESULTS_MARKER not in html:
        return OrderResult(NOT_RESULTS_PAGE)

    if NO_RECORD_MARKER in html:
        return OrderResult(NO_RECORD)

    table = soup.find(
        "table", class_="table table-bordered sc-table"
    ) or soup.find("table", class_="table")
    tbody = table.find("tbody") if table is not None else None
    if tbody is None:
        return OrderResult(NOT_RESULTS_PAGE)

    doc_urls: list[str] = []
    seen: set[str] = set()
    for row in tbody.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 10:
            continue
        link = cells[9].find("a", class_="download_content")
        href = link.get("href") if link else None
        if not href:
            continue
        url = urljoin(base_url, href)
        if url not in seen:
            seen.add(url)
            doc_urls.append(url)

    if not doc_urls:
        return OrderResult(NO_RECORD)
    return OrderResult(DOCS, doc_urls=doc_urls)


# ── DocumentSource shaping ───────────────────────────────────────────────────


def order_file_links(files: list[dict]) -> list[dict[str, str]]:
    """Roled links for a case's order files — the sole PDF-preferred primary is
    ``RAW``, every other file ``ALTERNATE`` (mirrors the retired
    ``document_sources.file_links``).

    ``files`` is a list of ``{"link": <durable url>, "is_pdf": bool}`` in download
    order. The primary is chosen by ``is_pdf`` (a stable flag) rather than the URL
    suffix, because the durable store hashes filenames so the link no longer ends
    ``.pdf``.
    """
    ordered = sorted(
        [f for f in files if f.get("link")], key=lambda f: 0 if f.get("is_pdf") else 1
    )
    return [
        {"link": f["link"], "role": "RAW" if i == 0 else "ALTERNATE"}
        for i, f in enumerate(ordered)
    ]


def build_order_document_source(
    court_identifier: str, case_number: str, files: list[dict]
) -> dict | None:
    """The canonical ``document_sources`` entry for a case's captured orders.

    Shape: ``{"document_id", "source_type": "COURT_ORDER", "url": [{"link",
    "role"}]}`` — ``url`` is the roled-link LIST (the shape read by
    ``media_objects_from_document_sources`` / ``court_order_to_jsonld`` and by the
    ``/documents`` API), NOT the retired spider's ``url``-scalar + ``links`` pair.
    Returns ``None`` when there are no usable files.
    """
    links = order_file_links(files)
    if not links:
        return None
    return {
        "document_id": f"ngm:court-order:{court_identifier}:{case_number}",
        "source_type": "COURT_ORDER",
        "url": links,
    }


# ── extra_data order-state transitions (mutate + return the dict) ─────────────

_TERMINAL_KEYS = (K_FAILED, K_ERROR, K_FAILED_AT)
_TOO_RECENT_KEYS = (K_TOO_RECENT, K_TOO_RECENT_AT)
_TRANSIENT_KEYS = (K_TRANSIENT_ERROR, K_TRANSIENT_AT, K_TRANSIENT_RETRIES)


def mark_success(
    extra_data: dict | None, *, links: list[str], now_iso: str
) -> dict:
    """Clear all failure/too-recent/transient state and stamp the capture.

    ``court_orders`` holds the durable file URLs (non-null → excluded from the
    backlog query); ``court_orders_scraped_at`` records when. Mirrors the retired
    pipeline's ``_mark_success`` minus the SQLAlchemy plumbing.
    """
    data = dict(extra_data or {})
    data[K_ORDERS] = list(links)
    data[K_SCRAPED_AT] = now_iso
    for key in (*_TERMINAL_KEYS, *_TOO_RECENT_KEYS, *_TRANSIENT_KEYS):
        data.pop(key, None)
    return data


def mark_too_recent(extra_data: dict | None, *, now_iso: str) -> dict:
    """Soft-skip: the case is too recent for a document to exist yet. Re-picked
    after ``TOO_RECENT_RECHECK_DAYS``; a later capture clears these markers."""
    data = dict(extra_data or {})
    data[K_TOO_RECENT] = True
    data[K_TOO_RECENT_AT] = now_iso
    return data


def mark_failed(extra_data: dict | None, *, error: str, now_iso: str) -> dict:
    """Permanent failure — excluded from all future runs until manually cleared."""
    data = dict(extra_data or {})
    data[K_FAILED] = True
    data[K_ERROR] = error
    data[K_FAILED_AT] = now_iso
    return data


def mark_transient(extra_data: dict | None, *, error: str, now_iso: str) -> dict:
    """Non-terminal failure (download/network) — bump the retry counter and leave
    the case re-crawlable, escalating to a permanent failure only after
    ``MAX_TRANSIENT_RETRIES`` so a genuinely-dead URL eventually stops re-queuing.
    """
    data = dict(extra_data or {})
    retries = int(data.get(K_TRANSIENT_RETRIES, 0) or 0) + 1
    data[K_TRANSIENT_RETRIES] = retries
    data[K_TRANSIENT_ERROR] = error
    data[K_TRANSIENT_AT] = now_iso
    for key in _TERMINAL_KEYS:
        data.pop(key, None)
    if retries >= MAX_TRANSIENT_RETRIES:
        data[K_FAILED] = True
        data[K_ERROR] = f"transient_exhausted after {retries} retries: {error}"
        data[K_FAILED_AT] = now_iso
    return data


def is_document_expected(last_hearing_date: date | None, *, today: date) -> bool:
    """True once the last hearing is old enough that an order document should be
    published — the line between a permanent no-document failure and a soft
    "too recent" re-check."""
    if last_hearing_date is None:
        return False
    return (today - last_hearing_date).days >= MIN_DAYS_FOR_DOCUMENT_AVAILABILITY


# ── backlog selection ────────────────────────────────────────────────────────

_CAPTURE_PRIORITY = Case(
    When(Q(court_id="special") & Q(case_number__regex=r"^\d+-CR-"), then=Value(1)),
    When(Q(court_id="supreme") & Q(case_number__regex=r"^\d+-WH-"), then=Value(2)),
    When(Q(court_id="supreme") & Q(case_number__regex=r"^\d+-WF-"), then=Value(3)),
    When(Q(court_id="supreme") & Q(case_number__regex=r"^\d+-WO-"), then=Value(4)),
    When(Q(court_id="special") & ~Q(case_number__regex=r"^\d+-OA-"), then=Value(5)),
    When(Q(court_id="supreme"), then=Value(6)),
    When(Q(court_id="special") & Q(case_number__regex=r"^\d+-OA-"), then=Value(7)),
    default=Value(99),
    output_field=IntegerField(),
)


def orders_backlog_queryset(
    *, courts: tuple[str, ...] = ORDER_COURTS, today: date
) -> QuerySet:
    """Decided Supreme/Special cases still needing an order-document fetch.

    Ports the retired spider's ``_get_cases_to_scrape`` to the Django ORM (``ngm``
    DB): decided (``case_status`` contains फैसला, not ongoing), not already
    captured (``court_orders`` null), not permanently failed, and a too-recent
    case only once its recheck window has elapsed. Ordered by capture priority
    then newest registration. Caller slices ``[:limit]``.
    """
    recheck_cutoff = (today - timedelta(days=TOO_RECENT_RECHECK_DAYS)).isoformat()

    # A negated JSON-key match (``~Q(key=True)`` / ``.exclude(key=True)``) DROPS a
    # row whose ``extra_data`` is a non-null dict that merely LACKS the key —
    # SQLite evaluates the absent key to NULL and treats ``NOT NULL`` as no-match,
    # and the behaviour differs from Postgres, so it is a cross-backend
    # correctness trap. ``__isnull=True`` on a JSON key DOES keep absent-key rows,
    # so both "not permanently failed" and "not recently soft-skipped" are
    # expressed as positive ``__isnull`` predicates. ``orders_failed`` is only
    # ever set to ``True`` or popped, so its absence ≡ "not failed".
    not_failed = Q(extra_data__orders_failed__isnull=True)
    # A soft-skipped case is eligible again once its recheck window has elapsed.
    # The timestamp is compared AS TEXT (``KeyTextTransform`` → JSON_EXTRACT text,
    # cast to text — mirroring the retired spider's ``.astext``); a raw JSON key
    # ``__lt`` compares with JSON-quoting semantics that break the ISO ordering
    # (and errors on SQLite).
    too_recent_ready = (
        Q(extra_data__orders_too_recent__isnull=True)
        | Q(_orders_too_recent_at__isnull=True)
        | Q(_orders_too_recent_at__lt=recheck_cutoff)
    )

    return (
        CourtCase.objects.using("ngm")
        .filter(court_id__in=courts, is_deleted=False)
        .annotate(
            _orders_too_recent_at=Cast(
                KeyTextTransform("orders_too_recent_checked_at", "extra_data"),
                TextField(),
            )
        )
        .filter(case_status__contains="फैसला")  # decided
        .exclude(case_status__contains="चालु")  # not ongoing (text field — safe)
        .exclude(case_status__contains="चलिरहेको")
        .filter(extra_data__court_orders__isnull=True)  # not already captured
        .filter(not_failed)
        .filter(too_recent_ready)
        .annotate(capture_priority=_CAPTURE_PRIORITY)
        .order_by("capture_priority", F("registration_date_ad").desc(nulls_last=True))
    )
