"""Submit mode: what gets POSTed, what gets skipped, and what stops a run."""

import logging
import urllib.error

import pytest

from casework import submit_reviews as sr

SLUG_A = "case-078-cr-0038-ciaa-special-court-case-078-cr-9a"
SLUG_B = "case-078-cr-0044-ciaa-special-court-case-078-cr-12"


class _StubApi:
    """Stands in for `CaseworkApi` at the two methods submit mode calls."""

    def __init__(self, reviews=None, errors=None):
        self.reviews = {k: list(v) for k, v in (reviews or {}).items()}
        self.errors = errors or {}
        self.submitted = []
        self._next_id = 1900

    def reviews_for_slug(self, slug):
        return list(self.reviews.get(slug, ()))

    def submit_review(self, slug):
        if slug in self.errors:
            raise self.errors[slug]
        self._next_id += 1
        row = {"id": self._next_id, "slug": slug, "status": "pending"}
        self.submitted.append(slug)
        self.reviews.setdefault(slug, []).insert(0, row)
        return row


def _http_error(code):
    return urllib.error.HTTPError(
        "http://127.0.0.1:48010/api/casework/reviews/submit/", code, "err", {}, None)


def _submit(api, slugs, tmp_path, **kw):
    """Call `submit_batch` with the run-logging arguments the CLI supplies."""
    events = tmp_path / "events.jsonl"
    events.touch()
    options = {"dry_run": False, "force": False}
    options.update(kw)
    return sr.submit_batch(api, slugs, logger=logging.getLogger("test"),
                           events_path=str(events), run_id="testrun", **options)


def test_a_never_reviewed_case_is_submitted(tmp_path):
    api = _StubApi()
    stats = _submit(api, [SLUG_A], tmp_path)
    assert api.submitted == [SLUG_A]
    assert stats["submitted"] == 1


def test_a_case_with_any_existing_review_is_skipped(tmp_path):
    api = _StubApi(reviews={SLUG_A: [{"id": 1841, "status": "done",
                                      "overall_score": 84, "disposition": "PASS"}]})
    stats = _submit(api, [SLUG_A, SLUG_B], tmp_path)
    assert api.submitted == [SLUG_B]
    assert stats["already_reviewed"] == 1
    assert stats["submitted"] == 1


def test_force_submits_a_case_that_already_has_a_review(tmp_path):
    api = _StubApi(reviews={SLUG_A: [{"id": 1841, "status": "done"}]})
    stats = _submit(api, [SLUG_A], tmp_path, force=True)
    assert api.submitted == [SLUG_A]
    assert stats["submitted"] == 1


def test_a_dry_run_posts_nothing(tmp_path):
    api = _StubApi()
    stats = _submit(api, [SLUG_A, SLUG_B], tmp_path, dry_run=True)
    assert api.submitted == []
    assert stats["would_submit"] == 2


def test_a_400_is_recorded_and_the_batch_continues(tmp_path):
    api = _StubApi(errors={SLUG_A: _http_error(400)})
    stats = _submit(api, [SLUG_A, SLUG_B], tmp_path)
    assert api.submitted == [SLUG_B]
    assert stats["error"] == 1
    assert stats["submitted"] == 1


def test_a_403_aborts_the_whole_run(tmp_path):
    """It will fail identically on every remaining case, so one clear error beats
    several hundred."""
    api = _StubApi(errors={SLUG_A: _http_error(403)})
    with pytest.raises(SystemExit, match="Caseworker role"):
        _submit(api, [SLUG_A, SLUG_B], tmp_path)
    assert api.submitted == []


def test_the_write_guard_aborts_the_run_rather_than_counting_it(tmp_path):
    """A non-loopback base URL without --allow-remote-writes raises before any
    socket opens. Counting it per-case would turn one configuration mistake into
    several hundred logged errors and a zero exit code."""
    api = _StubApi(errors={SLUG_A: RuntimeError(
        "refusing to write to non-loopback base_url")})
    with pytest.raises(RuntimeError, match="non-loopback"):
        _submit(api, [SLUG_A, SLUG_B], tmp_path)
    assert api.submitted == []


def test_a_run_with_no_batch_and_no_slug_is_refused():
    args = sr.build_parser().parse_args([])
    with pytest.raises(SystemExit, match="--batch-csv or --slug"):
        sr.slugs_for_run(args)


def test_limit_takes_the_batch_in_file_order(tmp_path):
    batch = tmp_path / "batch.csv"
    batch.write_text(f"slug\n{SLUG_A}\n{SLUG_B}\n", encoding="utf-8")
    args = sr.build_parser().parse_args(["--batch-csv", str(batch), "--limit", "1"])
    assert sr.slugs_for_run(args) == [SLUG_A]


class _ReportApi(_StubApi):
    """Adds the detail read the report makes for failed rows only."""

    def __init__(self, reviews=None, details=None):
        super().__init__(reviews=reviews)
        self.details = details or {}
        self.detail_calls = []

    def review_detail(self, review_id):
        self.detail_calls.append(review_id)
        return self.details.get(review_id, {})


def test_report_reads_score_and_disposition_off_the_list_row():
    api = _ReportApi(reviews={SLUG_A: [{"id": 1841, "status": "done",
                                        "overall_score": 84, "disposition": "PASS",
                                        "case_title": "ओक्सिजन प्लान्ट",
                                        "duration_seconds": 92.4}]})
    rows = sr.report_rows(api, [SLUG_A])
    assert rows[0]["review_id"] == 1841
    assert rows[0]["disposition"] == "PASS"
    assert rows[0]["score"] == 84
    assert api.detail_calls == []          # a done row needs no detail fetch


def test_report_fetches_the_error_only_for_failed_rows():
    api = _ReportApi(
        reviews={SLUG_A: [{"id": 1841, "status": "done"}],
                 SLUG_B: [{"id": 1842, "status": "failed"}]},
        details={1842: {"error": "convert failed: no MARKDOWN role\ntraceback…"}},
    )
    rows = sr.report_rows(api, [SLUG_A, SLUG_B])
    assert api.detail_calls == [1842]
    failed = [r for r in rows if r["status"] == "failed"][0]
    assert failed["error"] == "convert failed: no MARKDOWN role"


def test_a_batch_slug_with_no_review_is_reported_as_never_submitted():
    api = _ReportApi()
    rows = sr.report_rows(api, [SLUG_A])
    assert rows[0]["status"] == "never-submitted"
    assert sr.summarize(rows)["never_submitted"] == [SLUG_A]


def test_summary_counts_dispositions_and_scores():
    rows = [
        {"slug": SLUG_A, "status": "done", "score": 84, "disposition": "PASS"},
        {"slug": SLUG_B, "status": "done", "score": 58, "disposition": "REVISE"},
        {"slug": "case-c", "status": "pending", "score": None, "disposition": None},
    ]
    summary = sr.summarize(rows)
    assert summary["statuses"]["done"] == 2
    assert summary["dispositions"]["PASS"] == 1
    assert summary["scored"] == 2
    assert summary["avg"] == 71.0
    assert (summary["min"], summary["max"]) == (58, 84)


def test_the_rendered_report_names_every_case_and_the_totals():
    rows = [{"slug": SLUG_A, "review_id": 1841, "status": "done", "score": 84,
             "disposition": "PASS", "duration": 92.4, "title": "", "error": ""}]
    text = sr.render_report(rows, sr.summarize(rows),
                            base_url="http://127.0.0.1:48010/api",
                            run_id="testrun", batch="batch.csv")
    assert SLUG_A in text
    assert "PASS" in text
    assert "1841" in text
