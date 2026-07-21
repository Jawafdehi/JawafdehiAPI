# tests/casework/test_select_batch.py
import csv

import pytest

from casework.select_batch import (
    in_year_scope, parse_bigo, select_batch, write_batch,
)

HOST = "https://jawafdehi.org/material"
PR = f"{HOST}/ciaa_press_release/2037"
CO = f"{HOST}/court_order/special.078-cr-0042"


class FakeApi:
    """Scripts each slug's live case: slug -> (case_dict, etag)."""

    def __init__(self, cases):
        self.cases = cases
        self.reads = []

    def get_case_with_etag(self, slug, timeout=60):
        self.reads.append(slug)
        if slug not in self.cases:
            raise KeyError(slug)
        return self.cases[slug]


def _row(slug, cno, bigo, pr=PR, **extra):
    r = {"slug": slug, "court_case_no": cno, "bigo_npr": str(bigo),
         "press_release_iri": pr, "court_order_iri": "", "abhiyog_ag_iri": ""}
    r.update(extra)
    return r


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_parse_bigo_handles_commas_and_garbage():
    assert parse_bigo({"bigo_npr": "1,20,00,000"}) == 12000000.0
    assert parse_bigo({"bigo_npr": ""}) == 0.0
    assert parse_bigo({}) == 0.0


@pytest.mark.parametrize("cno,years,expected", [
    ("078-CR-0001", ["078", "079"], True),
    ("081-CR-0009", ["078", "079"], False),
    ("081-CR-0009", [], True),           # no filter -> everything in scope
])
def test_in_year_scope(cno, years, expected):
    assert in_year_scope(cno, years) is expected


# ---------------------------------------------------------------------------
# select_batch -- production decides, the CSV only proposes
# ---------------------------------------------------------------------------


def test_selects_only_draft_and_unbound_sorted_by_bigo():
    rows = [
        _row("a", "078-CR-0001", 100),   # DRAFT, unbound -> selected
        _row("b", "078-CR-0002", 900),   # DRAFT, unbound -> selected (higher bigo first)
        _row("c", "078-CR-0003", 500),   # PUBLISHED -> skipped
        _row("d", "078-CR-0004", 700),   # DRAFT but already bound -> skipped
    ]
    api = FakeApi({
        "a": ({"slug": "a", "state": "DRAFT", "evidence": []}, "e"),
        "b": ({"slug": "b", "state": "DRAFT", "evidence": []}, "e"),
        "c": ({"slug": "c", "state": "PUBLISHED", "evidence": []}, "e"),
        "d": ({"slug": "d", "state": "DRAFT",
               "evidence": [{"material_iri": PR, "additional_details": ""}]}, "e"),
    })
    selected, stats = select_batch(rows, api)
    assert [r["slug"] for r in selected] == ["b", "a"]   # bigo desc
    assert stats["selected"] == 2
    assert stats["not_draft"] == 1
    assert stats["already_bound"] == 1


def test_partial_evidence_is_not_treated_as_bound():
    # Case has an unrelated news item bound but still lacks its press release ->
    # it must be selected, not skipped (the over-filter bug missing_candidates
    # guards against).
    rows = [_row("a", "078-CR-0001", 100)]
    api = FakeApi({"a": ({"slug": "a", "state": "DRAFT",
                          "evidence": [{"material_iri": f"{HOST}/news/9",
                                        "additional_details": ""}]}, "e")})
    selected, stats = select_batch(rows, api)
    assert [r["slug"] for r in selected] == ["a"]
    assert stats["already_bound"] == 0


def test_year_scope_and_limit_and_drop_are_applied():
    rows = [
        _row("a", "078-CR-0001", 900),
        _row("b", "079-CR-0002", 800),
        _row("c", "081-CR-0003", 999),                 # out of year scope
        _row("d", "078-CR-0004", 700, match_tier="D_CONTRADICTED"),  # quarantined
    ]
    api = FakeApi({s: ({"slug": s, "state": "DRAFT", "evidence": []}, "e")
                   for s in ("a", "b", "d")})
    selected, stats = select_batch(
        rows, api, years=["078", "079"], limit=1,
        drops=[("match_tier", "D_CONTRADICTED")])
    assert [r["slug"] for r in selected] == ["a"]   # highest bigo within scope
    assert stats["out_of_year"] == 1
    assert stats["dropped"] == 1
    assert "c" not in api.reads and "d" not in api.reads  # never live-read


def test_limit_stops_after_enough_and_reads_high_bigo_first():
    rows = [_row(s, "078-CR-0001", bigo)
            for s, bigo in [("lo", 1), ("hi", 900), ("mid", 500)]]
    api = FakeApi({s: ({"slug": s, "state": "DRAFT", "evidence": []}, "e")
                   for s in ("lo", "hi", "mid")})
    selected, stats = select_batch(rows, api, limit=2)
    assert [r["slug"] for r in selected] == ["hi", "mid"]
    assert "lo" not in api.reads          # limit reached before the lowest-bigo read


def test_rows_without_slug_or_candidates_are_skipped_without_reading():
    rows = [
        {"slug": "", "court_case_no": "078-CR-0001", "bigo_npr": "9",
         "press_release_iri": PR},                       # no slug
        _row("nocands", "078-CR-0002", 9, pr=""),        # no candidate IRIs
    ]
    api = FakeApi({})
    selected, stats = select_batch(rows, api)
    assert selected == []
    assert stats["no_candidates"] == 2
    assert api.reads == []                # nothing live-read


def test_fetch_failure_counts_and_does_not_select():
    rows = [_row("gone", "078-CR-0001", 9)]
    api = FakeApi({})                     # get raises KeyError
    selected, stats = select_batch(rows, api)
    assert selected == []
    assert stats["fetch_failed"] == 1


# ---------------------------------------------------------------------------
# write_batch -- binder-ready CSV
# ---------------------------------------------------------------------------


def test_write_batch_emits_binder_columns(tmp_path):
    out = tmp_path / "batch.csv"
    rows = [_row("a", "078-CR-0001", 100, extra_col="ignored")]
    n = write_batch(rows, str(out))
    assert n == 1
    got = list(csv.DictReader(open(out, encoding="utf-8")))
    assert got[0]["slug"] == "a"
    assert got[0]["press_release_iri"] == PR
    assert "extra_col" not in got[0]      # only OUTPUT_COLUMNS are written
