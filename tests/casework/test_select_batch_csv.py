"""`--batch-csv` as a hard allowlist for enricher selection.

The operator requirement this file pins: when a batch CSV is supplied, a run
must touch ONLY the cases listed in it. Every test here exists because the
failure mode is silent -- an over-selecting run looks exactly like a correct
one in the console, and under `--apply` it writes to cases nobody reviewed.
"""

import argparse

import pytest

from casework.common.cli import add_common_args
from casework.common.select import (
    select_cases, select_for_run, slugs_from_batch_csv,
)

SPECIAL = "https://jawafdehi.org/courtcase/special/081-cr-0098"


def _args(argv):
    """Parse through the REAL shared parser, so a renamed/dropped flag fails
    here instead of drifting silently out of the enrichers."""
    return add_common_args(argparse.ArgumentParser()).parse_args(argv)


def _csv(tmp_path, text, name="batch.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


# --- reading the CSV -------------------------------------------------------

def test_batch_csv_preserves_file_order(tmp_path):
    # File order is load-bearing: `--limit N` must mean "the first N rows of
    # my batch", which is only true if this preserves order.
    path = _csv(tmp_path, "slug\nc-third\nc-first\nc-second\n")
    assert slugs_from_batch_csv(path) == ["c-third", "c-first", "c-second"]


def test_batch_csv_ignores_the_binders_extra_columns(tmp_path):
    # The whole point of reusing the binder's format is that
    # `select_batch.py` output feeds straight in. Those CSVs carry material
    # IRI columns alongside `slug`.
    path = _csv(
        tmp_path,
        "slug,ciaa_press_release,court_order\n"
        "case-078-cr-0001,https://jawafdehi.org/material/ciaa_press_release/1979,x\n",
    )
    assert slugs_from_batch_csv(path) == ["case-078-cr-0001"]


def test_batch_csv_skips_blank_rows_and_dedupes_keeping_first_position(tmp_path):
    path = _csv(tmp_path, "slug\na\n\n  \nb\na\n")
    assert slugs_from_batch_csv(path) == ["a", "b"]


def test_batch_csv_strips_surrounding_whitespace(tmp_path):
    # A hand-edited CSV picks up stray spaces; an unstripped slug matches no
    # case and silently shrinks the batch.
    path = _csv(tmp_path, "slug\n  case-078-cr-0001  \n")
    assert slugs_from_batch_csv(path) == ["case-078-cr-0001"]


def test_batch_csv_without_a_slug_column_fails_loud(tmp_path):
    path = _csv(tmp_path, "case,amount\nfoo,1\n")
    with pytest.raises(SystemExit, match="slug"):
        slugs_from_batch_csv(path)


def test_batch_csv_that_does_not_exist_fails_loud(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        slugs_from_batch_csv(str(tmp_path / "nope.csv"))


def test_batch_csv_with_no_usable_rows_fails_loud(tmp_path):
    # THE landmine. An empty batch must never reach `select_cases` as an
    # empty slug set, because that falls through to BULK selection -- i.e. a
    # typo'd or truncated batch file would silently enrich every enrichable
    # case in production instead of nothing.
    path = _csv(tmp_path, "slug\n\n   \n")
    with pytest.raises(SystemExit, match="no slugs"):
        slugs_from_batch_csv(path)


# --- selecting against the batch ------------------------------------------

def test_batch_never_selects_a_case_outside_it():
    # `outside` is DRAFT and matches the fiscal year, so bulk selection would
    # happily take it. The batch must exclude it anyway.
    cases = [
        {"slug": "inside", "state": "DRAFT", "court_cases": [SPECIAL]},
        {"slug": "outside", "state": "DRAFT", "court_cases": [SPECIAL]},
    ]
    got = select_cases(cases, batch_slugs=["inside"])
    assert [c["slug"] for c in got] == ["inside"]


def test_batch_returns_cases_in_batch_order_not_api_order():
    # The API returns newest-first; the batch file is ascending. Without this,
    # `--limit 10` against a 238-row batch gives whichever ten the API
    # happened to return first -- not the first ten rows the operator listed.
    cases = [
        {"slug": "b", "state": "DRAFT", "court_cases": [SPECIAL]},
        {"slug": "a", "state": "DRAFT", "court_cases": [SPECIAL]},
    ]
    got = select_cases(cases, batch_slugs=["a", "b"])
    assert [c["slug"] for c in got] == ["a", "b"]


def test_batch_still_applies_the_state_gate():
    # Deliberately STRICTER than the `--slug` bypass. A stale batch CSV that
    # still lists a case since PUBLISHED must not be enriched: over-selection
    # is invisible and writes to reviewed cases, under-selection is visible in
    # the n_selected count. Use `--slug` for a deliberate one-off override.
    cases = [
        {"slug": "draft", "state": "DRAFT", "court_cases": [SPECIAL]},
        {"slug": "pub", "state": "PUBLISHED", "court_cases": [SPECIAL]},
    ]
    got = select_cases(cases, batch_slugs=["draft", "pub"])
    assert [c["slug"] for c in got] == ["draft"]


def test_batch_slug_absent_from_the_api_payload_is_skipped_not_fatal():
    cases = [{"slug": "here", "state": "DRAFT", "court_cases": [SPECIAL]}]
    got = select_cases(cases, batch_slugs=["here", "deleted-since"])
    assert [c["slug"] for c in got] == ["here"]


def test_fiscal_year_can_only_narrow_a_batch_never_widen_it():
    # A caller passing both must not end up with the fiscal-year cohort.
    cases = [
        {"slug": "in-batch-081", "state": "DRAFT", "court_cases": [SPECIAL]},
        {"slug": "not-in-batch-081", "state": "DRAFT", "court_cases": [SPECIAL]},
    ]
    got = select_cases(cases, fiscal_year="081", batch_slugs=["in-batch-081"])
    assert [c["slug"] for c in got] == ["in-batch-081"]


def test_slug_flag_can_only_narrow_a_batch_never_widen_it():
    cases = [
        {"slug": "in-both", "state": "DRAFT", "court_cases": [SPECIAL]},
        {"slug": "batch-only", "state": "DRAFT", "court_cases": [SPECIAL]},
        {"slug": "flag-only", "state": "DRAFT", "court_cases": [SPECIAL]},
    ]
    got = select_cases(
        cases, slugs=("in-both", "flag-only"), batch_slugs=["in-both", "batch-only"],
    )
    assert [c["slug"] for c in got] == ["in-both"]


# --- the shared run-selection path the enrichers call ---------------------

def test_flag_is_registered_on_the_shared_parser(tmp_path):
    # A real path: the flag validates its argument at parse time, so a
    # placeholder name would fail here for the wrong reason.
    path = _csv(tmp_path, "slug\na\n")
    assert _args(["--batch-csv", path]).batch_csv == path


def test_no_batch_flag_leaves_bulk_selection_untouched():
    cases = [{"slug": "a", "state": "DRAFT", "court_cases": [SPECIAL]}]
    assert [c["slug"] for c in select_for_run(cases, _args([]))] == ["a"]


def test_select_for_run_limits_to_the_first_rows_of_the_batch(tmp_path):
    path = _csv(tmp_path, "slug\na\nb\nc\n")
    cases = [
        {"slug": s, "state": "DRAFT", "court_cases": [SPECIAL]}
        for s in ("c", "b", "a")  # API order: reverse of the batch
    ]
    args = _args(["--batch-csv", path, "--limit", "2"])
    assert [c["slug"] for c in select_for_run(cases, args)] == ["a", "b"]


def test_select_for_run_honours_limit_without_a_batch():
    cases = [
        {"slug": f"c{i}", "state": "DRAFT", "court_cases": [SPECIAL]}
        for i in range(5)
    ]
    got = select_for_run(cases, _args(["--limit", "2"]))
    assert len(got) == 2
