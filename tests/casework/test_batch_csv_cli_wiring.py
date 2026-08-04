"""Tests for how `--batch-csv` is wired into the argparse layer.

`test_select_batch_csv.py` covers the selection algebra. This file covers the
three ways the flag can be wrong before selection ever runs:

- `bind_materials.py` registers its own required `--batch-csv`, so once
  `add_common_args` also registers one, its parser cannot even be built.
- `--batch-csv "$BATCH"` with an unset variable arrives as the empty string,
  and an empty allowlist that falls through to bulk selection is the exact
  opposite of what the flag is for.
- `convert.py` calls `add_common_args`, so it advertises `--batch-csv` in
  `--help`; it must honour it rather than convert the whole corpus.
"""
import argparse
import types

import pytest

from casework import bind_materials
from casework.common.cli import add_common_args
from casework.common.select import select_for_run
from casework.convert import slugs_for_run


def _batch(tmp_path, *slugs):
    path = tmp_path / "batch.csv"
    path.write_text("slug\n" + "".join(f"{s}\n" for s in slugs), encoding="utf-8")
    return str(path)


def _parser():
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    return parser


# ---------------------------------------------------------------------------
# bind_materials.py must keep working -- it is the tool whose CSV format the
# enrichers' --batch-csv deliberately matches.
# ---------------------------------------------------------------------------


def test_bind_materials_parser_can_be_built():
    """Regression: a duplicate --batch-csv makes build_parser() raise
    ArgumentError before it parses anything, killing every invocation."""
    assert bind_materials.build_parser() is not None


def test_bind_materials_still_requires_batch_csv():
    with pytest.raises(SystemExit):
        bind_materials.build_parser().parse_args([])


def test_bind_materials_accepts_a_batch_csv(tmp_path):
    path = _batch(tmp_path, "case-078-cr-0001-ciaa-")
    args = bind_materials.build_parser().parse_args(["--batch-csv", path])
    assert args.batch_csv == path


def test_bind_materials_batch_csv_help_still_mentions_material_columns():
    """bind_materials consumes material-IRI columns the enrichers ignore, so it
    must not inherit the enrichers' slug-only help text."""
    action = next(a for a in bind_materials.build_parser()._actions
                  if a.dest == "batch_csv")
    assert "material" in (action.help or "").lower()


# ---------------------------------------------------------------------------
# An empty or missing path must never degrade into a full-corpus run.
# ---------------------------------------------------------------------------


def test_empty_batch_csv_value_is_rejected_at_parse_time():
    """`--batch-csv "$BATCH"` with $BATCH unset. argparse accepts the empty
    string happily, and select_for_run then treats it as 'no batch'."""
    with pytest.raises(SystemExit):
        _parser().parse_args(["--batch-csv", ""])


def test_whitespace_only_batch_csv_value_is_rejected():
    with pytest.raises(SystemExit):
        _parser().parse_args(["--batch-csv", "   "])


def test_missing_batch_csv_file_is_rejected_at_parse_time(tmp_path):
    """A typo must cost nothing, not ~16 pages of list_cases first."""
    with pytest.raises(SystemExit):
        _parser().parse_args(["--batch-csv", str(tmp_path / "nope.csv")])


def test_absent_batch_csv_still_means_bulk_selection():
    """The guard must not break the no-batch path."""
    args = _parser().parse_args([])
    cases = [{"slug": f"s{i}", "state": "DRAFT"} for i in range(3)]
    assert len(select_for_run(cases, args)) == 3


def test_select_for_run_refuses_an_empty_batch_from_a_hand_built_namespace():
    """Defence in depth for programmatic callers that bypass argparse: an empty
    allowlist must fail loud, never widen to the whole corpus."""
    args = types.SimpleNamespace(
        batch_csv="", fiscal_year="", slug=[], court_case=[], limit=0)
    cases = [{"slug": f"s{i}", "state": "DRAFT"} for i in range(3)]
    with pytest.raises(SystemExit):
        select_for_run(cases, args)


# ---------------------------------------------------------------------------
# convert.py advertises the flag, so it must honour it.
# ---------------------------------------------------------------------------


class _FakeApi:
    def __init__(self, *slugs):
        self._slugs = slugs

    def iter_cases(self):
        return [{"slug": s, "state": "DRAFT"} for s in self._slugs]


def _convert_args(**kw):
    base = dict(batch_csv=None, slug=[], limit=0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_convert_honours_batch_csv(tmp_path):
    path = _batch(tmp_path, "case-a", "case-b")
    api = _FakeApi("case-a", "case-b", "case-zzz-not-in-batch")
    assert slugs_for_run(api, _convert_args(batch_csv=path)) == ["case-a", "case-b"]


def test_convert_batch_csv_never_admits_a_case_outside_the_file(tmp_path):
    path = _batch(tmp_path, "case-a")
    api = _FakeApi("case-a", "case-zzz-not-in-batch")
    assert "case-zzz-not-in-batch" not in slugs_for_run(
        api, _convert_args(batch_csv=path))


def test_convert_batch_csv_limit_takes_the_files_first_rows(tmp_path):
    path = _batch(tmp_path, "case-a", "case-b", "case-c")
    api = _FakeApi("case-c", "case-b", "case-a")
    assert slugs_for_run(api, _convert_args(batch_csv=path, limit=2)) == [
        "case-a", "case-b"]


def test_convert_without_batch_csv_still_walks_every_case():
    api = _FakeApi("case-a", "case-b")
    assert slugs_for_run(api, _convert_args()) == ["case-a", "case-b"]


def test_convert_slug_flag_still_wins_over_a_full_walk():
    api = _FakeApi("case-a", "case-b")
    assert slugs_for_run(api, _convert_args(slug=["case-b"])) == ["case-b"]
