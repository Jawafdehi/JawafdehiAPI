# tests/casework/test_source_abhiyog.py
import json
import urllib.error

import pytest

from casework.source_abhiyog import (
    build_rows, canonical_case_no, extract_case_no, fiscal_year, summarise,
    validate_records, write_outputs,
)


def record(**over):
    """A portal record shaped like the ag.gov.np search response."""
    base = {
        "id": 83475,
        "name": "जिल्ला कालिकोट",
        "description": "भ्रष्ट्राचार गरेको सम्बन्धमा (०८१-CR-००९४)",
        "file": "081-CR-0094_1750232440.pdf",
        "court_case_no": None,
        "created_date_np": "2082-3-4",
        "month": {"year": {"name": "2082"}},
    }
    base.update(over)
    return base


class FakeApi:
    """Scripts material existence the way the real probe sees it."""

    def __init__(self, exists=(), absent=(), uncertain=()):
        self.exists, self.absent = set(exists), set(absent)
        self.uncertain = set(uncertain)
        self.calls = []

    def get(self, path, timeout=60):
        self.calls.append(path)
        ident = path.removeprefix("/materials/ag/").rstrip("/")
        if ident in self.uncertain:
            raise urllib.error.URLError("boom")
        if ident in self.exists:
            return {"@id": ident}
        raise urllib.error.HTTPError(path, 404, "nf", {}, None)


# --------------------------------------------------------------------------
# canonicalisation
# --------------------------------------------------------------------------

def test_canonical_transliterates_devanagari_and_upcases_type():
    assert canonical_case_no("०८१-cr-००९४") == "081-CR-0094"


def test_canonical_preserves_leading_zeros():
    # 0094 must not collapse to 94 -- leading zeros are significant.
    assert canonical_case_no("081-CR-0094") == "081-CR-0094"


def test_canonical_rejects_non_case_numbers():
    for junk in ("", None, "not-a-case", "81-CR-94x", "बैंकिङ्ग कसूर"):
        assert canonical_case_no(junk) is None


# --------------------------------------------------------------------------
# extraction + cross-validation
# --------------------------------------------------------------------------

def test_court_case_no_wins_over_file_and_description():
    case_no, source, cands, agree = extract_case_no(
        record(court_case_no="082-FT-0442"))
    assert (case_no, source) == ("082-FT-0442", "court_case_no")
    # all three still reported, so a disagreement is visible
    assert set(cands) == {"court_case_no", "file", "description"}
    assert agree is False


def test_file_wins_over_description_when_court_case_no_missing():
    case_no, source, _, agree = extract_case_no(record())
    assert (case_no, source, agree) == ("081-CR-0094", "file", True)


def test_description_used_when_filename_has_no_case_number():
    case_no, source, _, _ = extract_case_no(
        record(file="प्रतिवादी सरस्वती खड्का_1719399807.pdf"))
    assert (case_no, source) == ("081-CR-0094", "description")


def test_disagreement_is_flagged_not_silently_resolved():
    case_no, _, cands, agree = extract_case_no(
        record(file="080-CR-0009_1.pdf",
               description="घुस लिई (०८१-CR-०००९)"))
    assert agree is False
    assert case_no == "080-CR-0009"          # priority pick
    assert cands["description"] == "081-CR-0009"  # rejected value retained


def test_unrecoverable_record_yields_no_case_number():
    case_no, source, cands, agree = extract_case_no(record(
        file="लक्ष्मण चालिसे_1642407863.pdf", description="बैंकिङ्ग कसुर"))
    assert (case_no, source, cands, agree) == (None, None, {}, None)


def test_fiscal_year_reads_nested_month_year():
    assert fiscal_year(record()) == "2082"
    assert fiscal_year({"month": None}) == ""


# --------------------------------------------------------------------------
# rows + lake probe
# --------------------------------------------------------------------------

def test_build_rows_marks_in_lake_and_absent():
    rows = build_rows([record(id=1), record(id=2)],
                      FakeApi(exists=["1"]), interval=0)
    assert [r["in_lake"] for r in rows] == ["true", "false"]
    assert rows[0]["material_iri"] == "https://jawafdehi.org/material/ag/1"


def test_build_rows_can_skip_probe_entirely():
    rows = build_rows([record()], api=None, probe=False)
    assert rows[0]["in_lake"] == ""


def test_uncertain_probe_reports_error_not_a_false_absent():
    # A throttled/5xx read must never be recorded as "definitely not in lake".
    rows = build_rows([record(id=7)], FakeApi(uncertain=["7"]), interval=0)
    assert rows[0]["in_lake"] == "error"


def test_record_without_id_is_dropped_not_emitted_as_ag_none():
    # ag_ident(None) would build `/material/ag/None`; such a row must not exist.
    rows = build_rows([record(id=None), record(id=5)], api=None, probe=False)
    assert [r["id"] for r in rows] == [5]


def test_probe_cache_is_reused_and_not_reprobed():
    api = FakeApi(exists=["1"])
    cache = {}
    build_rows([record(id=1)], api, interval=0, probe_cache=cache)
    assert cache == {"1": "true"} and len(api.calls) == 1
    build_rows([record(id=1)], api, interval=0, probe_cache=cache)
    assert len(api.calls) == 1  # second run served from cache


def test_probe_cache_retries_a_previously_errored_verdict():
    # An "error" is not an answer -- a re-run must try it again.
    api = FakeApi(exists=["1"])
    cache = {"1": "error"}
    build_rows([record(id=1)], api, interval=0, probe_cache=cache)
    assert cache["1"] == "true"


def test_ambiguous_row_carries_the_alternate():
    rows = build_rows([record(file="080-CR-0009_1.pdf",
                              description="(०८१-CR-०००९)")],
                      api=None, probe=False)
    assert rows[0]["ambiguous"] == "true"
    assert rows[0]["alt_case_number"] == "081-CR-0009"


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------

def test_validate_records_rejects_a_corrupt_cache():
    # A truncated/hand-edited snapshot must fail loudly, not iterate as keys.
    for bad in ({}, [], {"a": {"id": 1}}, [1, 2], None):
        with pytest.raises(SystemExit):
            validate_records(bad, "cache")


def test_validate_records_accepts_a_real_cohort():
    assert validate_records([record()], "cache") == [record()]


def test_write_outputs_appends_rather_than_replacing_a_dotted_suffix(tmp_path):
    # `Path.with_suffix` would collapse map.v2 -> map.csv, clobbering map.v1.
    csv_path, json_path = write_outputs([], tmp_path / "map.v2")
    assert csv_path.name == "map.v2.csv"
    assert json_path.name == "map.v2.json"


def test_write_outputs_round_trips_devanagari(tmp_path):
    rows = build_rows([record()], api=None, probe=False)
    csv_path, json_path = write_outputs(rows, tmp_path / "map")
    assert csv_path.exists() and json_path.exists()
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded[0]["name"] == "जिल्ला कालिकोट"
    assert "जिल्ला" in csv_path.read_text(encoding="utf-8")


def test_summarise_counts_each_bucket():
    rows = build_rows(
        [record(id=1), record(id=2, file="x_1.pdf", description="बैंकिङ्ग")],
        FakeApi(exists=["1"]), interval=0)
    stats = summarise(rows)
    assert stats["records"] == 2
    assert stats["recovered"] == 1
    assert stats["unrecoverable"] == 1
    assert stats["in_lake"] == 1
    assert stats["not_in_lake"] == 1
