"""Court-order capture (``courts.scraper.orders`` + ``scrape_court_orders``).

The pure halves — CAPTCHA-cookie parse, result-page parse, DocumentSource
shaping, the ``extra_data`` state machine — are exercised with fabricated portal
HTML matching the retired ``supreme_court_orders`` spider's contract. The backlog
selection and the command's download→store→materialize path run against the
sqlite fallback (managed court tables), with the HTTP transport and durable store
faked so the test is hermetic.

Run (DB-less) from the repo root::

    SECRET_KEY=dev ALLOWED_HOSTS='*' uv run pytest courts/tests/test_scraper_orders.py
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from courts.models import Court, CourtCase, CourtCaseHearing
from courts.scraper import orders as O
from materials.jsonld import build_courtcase_iri, court_order_material_iri
from materials.models import Material

CMD = "courts.management.commands.scrape_court_orders"

# ── portal HTML fixtures ─────────────────────────────────────────────────────

# A valid results page carrying two order files: a PDF (→ RAW) and a DOCX
# (→ ALTERNATE), each an <a class="download_content"> in the 10th cell (index 9).
_ROW = (
    "<tr>"
    + "".join(f"<td>c{i}</td>" for i in range(9))
    + '<td><a class="download_content" href="{href}">डाउनलोड</a></td>'
    + "</tr>"
)
DOCS_HTML = (
    "<html><body><h3>फैसला / आदेश को पुर्ण पाठ</h3>"
    '<table class="table table-bordered sc-table"><tbody>'
    + _ROW.format(href="/court/media/2081/order.pdf")
    + _ROW.format(href="/court/media/2081/order.docx")
    + "</tbody></table></body></html>"
)
NO_RECORD_HTML = (
    "<html><body><h3>फैसला / आदेश को पुर्ण पाठ</h3>"
    "<p>रेकर्ड भेटिएन</p></body></html>"
)
INVALID_CAPTCHA_HTML = (
    '<html><body><table bgcolor="#FF6600"><tr><td>Invalid CAPTCHA</td></tr>'
    "</table></body></html>"
)
SERVER_ERROR_HTML = (
    '<html><body><table bgcolor="#FF6600"><tr><td>Database error</td></tr>'
    "</table></body></html>"
)
NOT_RESULTS_HTML = "<html><body><p>माफ गर्नुहोस्</p></body></html>"

# A court_session cookie value embedding the CAPTCHA answer (PHP-serialised).
COOKIE = 'a:2:{s:12:"captcha_word";s:5:"AB12X";s:2:"id";s:1:"x";}'


# ── pure: form params + CAPTCHA ──────────────────────────────────────────────


def test_court_form_params():
    assert O.court_form_params("supreme") == ("S", "264")
    assert O.court_form_params("special") == ("T", "116")


def test_court_form_params_rejects_non_order_court():
    for bad in ("kathmandudc", "patanhc", ""):
        try:
            O.court_form_params(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_extract_captcha():
    assert O.extract_captcha(COOKIE) == "AB12X"
    assert O.extract_captcha("no-captcha-here") is None
    assert O.extract_captcha(None) is None
    assert O.extract_captcha("") is None


def test_order_form_data_shape():
    form = O.order_form_data("supreme", "082-WO-0123", "2081-05-12", "AB12X")
    assert form == {
        "court_type": "S",
        "court_id": "264",
        "regno": "082-WO-0123",
        "darta_date": "2081-05-12",
        "faisala_date": "",
        "captcha": "AB12X",
        "submit": "submit",
    }
    assert O.order_form_data("special", "x", None, "z")["darta_date"] == ""


# ── pure: result-page parse ──────────────────────────────────────────────────


def test_parse_docs_extracts_absolute_urls():
    result = O.parse_order_results(DOCS_HTML)
    assert result.status == O.DOCS
    assert result.doc_urls == [
        "https://supremecourt.gov.np/court/media/2081/order.pdf",
        "https://supremecourt.gov.np/court/media/2081/order.docx",
    ]


def test_parse_no_record():
    assert O.parse_order_results(NO_RECORD_HTML).status == O.NO_RECORD


def test_parse_invalid_captcha():
    assert O.parse_order_results(INVALID_CAPTCHA_HTML).status == O.INVALID_CAPTCHA


def test_parse_server_error():
    assert O.parse_order_results(SERVER_ERROR_HTML).status == O.SERVER_ERROR


def test_parse_not_results_page():
    assert O.parse_order_results(NOT_RESULTS_HTML).status == O.NOT_RESULTS_PAGE
    assert O.parse_order_results("").status == O.NOT_RESULTS_PAGE


def test_parse_dedups_and_skips_short_rows():
    html = (
        "<html><body>फैसला / आदेश को पुर्ण पाठ"
        '<table class="table"><tbody>'
        # a short row (< 10 cells) is ignored
        "<tr><td>only</td><td>two</td></tr>"
        + _ROW.format(href="/d/1.pdf")
        + _ROW.format(href="/d/1.pdf")  # duplicate href → deduped
        + "</tbody></table></body></html>"
    )
    result = O.parse_order_results(html)
    assert result.status == O.DOCS
    assert result.doc_urls == ["https://supremecourt.gov.np/d/1.pdf"]


def test_parse_valid_page_no_download_links_is_no_record():
    html = (
        "<html><body>फैसला / आदेश को पुर्ण पाठ"
        '<table class="table"><tbody><tr>'
        + "".join(f"<td>c{i}</td>" for i in range(11))
        + "</tr></tbody></table></body></html>"
    )
    assert O.parse_order_results(html).status == O.NO_RECORD


# ── pure: DocumentSource shaping ─────────────────────────────────────────────


def test_order_file_links_pdf_is_raw_rest_alternate():
    links = O.order_file_links(
        [
            {"link": "https://s3/x/a", "is_pdf": False},
            {"link": "https://s3/x/b", "is_pdf": True},
            {"link": "https://s3/x/c", "is_pdf": False},
        ]
    )
    assert links[0] == {"link": "https://s3/x/b", "role": "RAW"}  # pdf floated
    assert [lk["role"] for lk in links[1:]] == ["ALTERNATE", "ALTERNATE"]


def test_order_file_links_empty():
    assert O.order_file_links([]) == []
    assert O.order_file_links([{"link": ""}]) == []


def test_build_order_document_source_canonical_shape():
    src = O.build_order_document_source(
        "supreme",
        "082-WO-0123",
        [
            {"link": "https://s3/1.docx", "is_pdf": False},
            {"link": "https://s3/2.pdf", "is_pdf": True},
        ],
    )
    assert src["document_id"] == "ngm:court-order:supreme:082-WO-0123"
    assert src["source_type"] == "COURT_ORDER"
    # url is the roled-link LIST (the monolith-canonical shape), PDF-first RAW.
    assert src["url"] == [
        {"link": "https://s3/2.pdf", "role": "RAW"},
        {"link": "https://s3/1.docx", "role": "ALTERNATE"},
    ]


def test_build_order_document_source_empty():
    assert O.build_order_document_source("supreme", "x", []) is None


# ── pure: extra_data state machine ───────────────────────────────────────────

_NOW = "2026-07-24T10:00:00"


def test_mark_success_clears_prior_state():
    prior = {
        "orders_failed": True,
        "orders_error": "old",
        "orders_too_recent": True,
        "orders_transient_retries": 3,
    }
    data = O.mark_success(prior, links=["https://s3/a", "https://s3/b"], now_iso=_NOW)
    assert data["court_orders"] == ["https://s3/a", "https://s3/b"]
    assert data["court_orders_scraped_at"] == _NOW
    for key in ("orders_failed", "orders_error", "orders_too_recent",
                "orders_transient_retries"):
        assert key not in data


def test_mark_too_recent():
    data = O.mark_too_recent(None, now_iso=_NOW)
    assert data["orders_too_recent"] is True
    assert data["orders_too_recent_checked_at"] == _NOW


def test_mark_failed():
    data = O.mark_failed(None, error="no_document_old_case", now_iso=_NOW)
    assert data["orders_failed"] is True
    assert data["orders_error"] == "no_document_old_case"


def test_mark_transient_escalates_after_max_retries():
    data = None
    for _ in range(O.MAX_TRANSIENT_RETRIES - 1):
        data = O.mark_transient(data, error="timeout", now_iso=_NOW)
        assert "orders_failed" not in data  # still re-crawlable
    data = O.mark_transient(data, error="timeout", now_iso=_NOW)
    assert data["orders_transient_retries"] == O.MAX_TRANSIENT_RETRIES
    assert data["orders_failed"] is True  # escalated


def test_is_document_expected():
    today = date(2026, 7, 24)
    assert O.is_document_expected(date(2024, 1, 1), today=today) is True
    assert O.is_document_expected(date(2026, 7, 1), today=today) is False
    assert O.is_document_expected(None, today=today) is False


# ── DB: backlog selection + the command ──────────────────────────────────────

_TODAY = date(2026, 7, 24)


class _FakeClient:
    """A scripted stand-in for ``OrdersHttpClient`` (no network)."""

    def __init__(self, *, homepage, search, downloads=None):
        self._homepage = homepage
        self._search = search
        self._downloads = downloads or {}
        self.forms = []

    def new_session(self):
        pass

    def homepage(self):
        return self._homepage

    def search(self, form):
        self.forms.append(form)
        return self._search

    def download(self, url):
        return self._downloads.get(url, (404, b""))


def _fake_store(uploaded_file, role="RAW"):
    return {"link": f"https://s3.jawafdehi.org/case_uploads/{uploaded_file.name}", "role": role}


class OrdersBacklogTests(TestCase):
    databases = "__all__"

    @classmethod
    def setUpTestData(cls):
        cls.supreme = Court.objects.create(
            identifier="supreme", court_type="supreme", full_name_nepali="सर्वोच्च"
        )
        cls.special = Court.objects.create(
            identifier="special", court_type="special", full_name_nepali="विशेष"
        )
        cls.district = Court.objects.create(
            identifier="kathmandudc", court_type="district", full_name_nepali="जिल्ला"
        )

        def mk(court, num, status="फैसला भएको", **extra):
            return CourtCase.objects.create(
                court=court, case_number=num, case_status=status,
                registration_date_ad=date(2024, 1, 1), extra_data=extra or None,
            )

        cls.eligible_special_cr = mk(cls.special, "082-CR-0001")   # priority 1
        cls.eligible_supreme_wo = mk(cls.supreme, "082-WO-0001")   # priority 4
        # excluded: not decided
        mk(cls.supreme, "082-WO-0002", status="चालु")
        # excluded: ongoing wording despite फैसला
        mk(cls.supreme, "082-WO-0003", status="फैसला चलिरहेको")
        # excluded: already captured
        mk(cls.supreme, "082-WO-0004", court_orders=["https://s3/x"])
        # excluded: permanently failed
        mk(cls.supreme, "082-WO-0005", orders_failed=True)
        # excluded: district court (no orders on this portal)
        mk(cls.district, "082-CR-0009")
        # too-recent but overdue for recheck → INCLUDED
        cls.recheck = mk(
            cls.supreme, "082-WO-0006",
            orders_too_recent=True, orders_too_recent_checked_at="2026-01-01T00:00:00",
        )
        # too-recent, checked yesterday → still excluded
        mk(
            cls.supreme, "082-WO-0007",
            orders_too_recent=True, orders_too_recent_checked_at="2026-07-23T00:00:00",
        )
        # transient failure short of the permanent-fail threshold → re-crawlable
        cls.transient = mk(cls.supreme, "082-WO-0008", orders_transient_retries=2)

    def test_backlog_selects_and_prioritises(self):
        rows = list(O.orders_backlog_queryset(today=_TODAY))
        keys = [(c.court_id, c.case_number) for c in rows]
        assert ("special", "082-CR-0001") in keys
        assert ("supreme", "082-WO-0001") in keys
        assert ("supreme", "082-WO-0006") in keys  # recheck window elapsed
        assert ("supreme", "082-WO-0002") not in keys  # not decided
        assert ("supreme", "082-WO-0003") not in keys  # ongoing
        assert ("supreme", "082-WO-0004") not in keys  # captured
        assert ("supreme", "082-WO-0005") not in keys  # failed
        assert ("kathmandudc", "082-CR-0009") not in keys  # wrong court
        assert ("supreme", "082-WO-0007") not in keys  # recheck window open
        assert ("supreme", "082-WO-0008") in keys  # transient (not yet permanent)
        # Special/CR (priority 1) sorts ahead of Supreme/WO (priority 4).
        assert keys.index(("special", "082-CR-0001")) < keys.index(("supreme", "082-WO-0001"))

    def test_court_filter_scopes_to_one_court(self):
        rows = list(O.orders_backlog_queryset(courts=("special",), today=_TODAY))
        assert {c.court_id for c in rows} == {"special"}


class OrdersCommandTests(TestCase):
    databases = "__all__"

    def setUp(self):
        self.court = Court.objects.create(
            identifier="supreme", court_type="supreme", full_name_nepali="सर्वोच्च"
        )
        self.case = CourtCase.objects.create(
            court=self.court, case_number="082-WO-0123", case_status="फैसला भएको",
            registration_date_bs="2081-05-12", registration_date_ad=date(2024, 6, 1),
        )

    def _run(self, client, *extra):
        with patch(f"{CMD}.build_http_client", return_value=client), patch(
            f"{CMD}.store_file_as_link", side_effect=_fake_store
        ), patch("time.sleep"), patch.dict(
            "os.environ", {"ENABLE_COURT_ORDER_CAPTURE": "1"}
        ):
            call_command(
                "scrape_court_orders", "--write", "--delay", "0",
                "--case", "supreme:082-WO-0123", *extra,
            )

    def test_capture_gate_refuses_without_optin(self):
        from django.core.management.base import CommandError
        with patch.dict("os.environ", {"ENABLE_COURT_ORDER_CAPTURE": ""}):
            try:
                call_command("scrape_court_orders", "--case", "supreme:082-WO-0123")
            except CommandError:
                return
        raise AssertionError("expected the capture opt-in gate to refuse")

    def _reload(self):
        return CourtCase.objects.using("ngm").get(
            court_id="supreme", case_number="082-WO-0123"
        )

    def test_captures_documents_and_materializes(self):
        pdf, docx = b"%PDF-1.4 bytes", b"docx bytes"
        client = _FakeClient(
            homepage=(200, COOKIE, None),
            search=(200, DOCS_HTML, None),
            downloads={
                "https://supremecourt.gov.np/court/media/2081/order.pdf": (200, pdf),
                "https://supremecourt.gov.np/court/media/2081/order.docx": (200, docx),
            },
        )
        self._run(client)

        case = self._reload()
        # the POST carried the extracted CAPTCHA + Supreme form params
        assert client.forms[0]["captcha"] == "AB12X"
        assert client.forms[0]["court_type"] == "S"

        # canonical DocumentSource (url = roled link LIST, PDF-first RAW)
        src = case.document_sources[0]
        assert src["document_id"] == "ngm:court-order:supreme:082-WO-0123"
        assert [lk["role"] for lk in src["url"]] == ["RAW", "ALTERNATE"]
        assert src["url"][0]["link"].endswith("supreme-082-WO-0123.1.pdf")

        # extra_data marked captured, no failure residue
        assert case.extra_data["court_orders"]
        assert "orders_failed" not in case.extra_data

        # the standalone court_order Material, linked to the ngm case + provenance
        material = Material.objects.using("ngm").get(
            iri=court_order_material_iri("supreme", "082-WO-0123")
        )
        data = material.data
        assert data["isPartOf"]["@id"] == build_courtcase_iri("supreme", "082-WO-0123")
        media = data["associatedMedia"]
        assert len(media) == 2
        raw = next(m for m in media if m["jawafdehi:linkRole"] == "RAW")
        import hashlib

        assert raw["jawafdehi:provenance"]["sha256"] == hashlib.sha256(pdf).hexdigest()

    def test_no_record_old_case_marks_failed(self):
        CourtCaseHearing.objects.create(
            court=self.court, case_number="082-WO-0123",
            hearing_date_bs="2078-01-01", hearing_date_ad=date(2021, 4, 14),
            scraped_at="2026-01-01T00:00:00Z",
        )
        client = _FakeClient(homepage=(200, COOKIE, None), search=(200, NO_RECORD_HTML, None))
        self._run(client)
        case = self._reload()
        assert case.extra_data["orders_failed"] is True
        assert case.document_sources in (None, [])

    def test_no_record_recent_case_marks_too_recent(self):
        CourtCaseHearing.objects.create(
            court=self.court, case_number="082-WO-0123",
            hearing_date_bs="2083-03-01", hearing_date_ad=date(2026, 6, 15),
            scraped_at="2026-06-15T00:00:00Z",
        )
        client = _FakeClient(homepage=(200, COOKIE, None), search=(200, NO_RECORD_HTML, None))
        self._run(client)
        case = self._reload()
        assert case.extra_data["orders_too_recent"] is True
        assert "orders_failed" not in case.extra_data

    def test_download_failure_marks_transient_and_leaves_recrawlable(self):
        # A found document that fails to download → no capture, case left
        # re-crawlable (transient), NOT permanently failed and NOT captured.
        client = _FakeClient(
            homepage=(200, COOKIE, None),
            search=(200, DOCS_HTML, None),
            downloads={"https://supremecourt.gov.np/court/media/2081/order.pdf": (503, b"")},
        )
        self._run(client)
        case = self._reload()
        assert case.extra_data["orders_transient_retries"] == 1
        assert "orders_failed" not in case.extra_data
        assert "court_orders" not in case.extra_data
        assert case.document_sources in (None, [])
        assert not Material.objects.using("ngm").filter(
            iri=court_order_material_iri("supreme", "082-WO-0123")
        ).exists()
