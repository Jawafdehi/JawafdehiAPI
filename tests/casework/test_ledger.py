"""Tests for the enrichment ledger consolidator (casework/ledger.py).

The ledger is READ-ONLY over the per-run event logs (see
casework/common/cli.py::log_event). It never touches the API and never re-runs
an enricher. These tests build synthetic *.events.jsonl files and assert the
fold: latest decisive outcome per (slug, stage).
"""
import json

from casework.ledger import (
    OUTCOME_STATUSES,
    build_ledger,
    iter_events,
    main,
    stage_summary,
    write_ledger,
)


def _write_events(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _ev(ts, stage, slug, step, status, detail=""):
    return {"ts": ts, "run_id": ts[:8], "stage": stage, "slug": slug,
            "step": step, "status": status, "detail": detail, "elapsed_ms": None}


class TestBuildLedger:
    def test_latest_outcome_wins_across_runs(self, tmp_path):
        # run 1 (earlier): bigo enriched. run 2 (later): idempotency 'already'
        # -- current state is 'already' (the field is now populated).
        _write_events(tmp_path / "a-bigo.events.jsonl", [
            _ev("2026-07-20T10:00:00Z", "bigo", "case-1", "write", "enriched", "5000000"),
        ])
        _write_events(tmp_path / "b-bigo.events.jsonl", [
            _ev("2026-07-21T10:00:00Z", "bigo", "case-1", "idempotency", "already"),
        ])
        led = build_ledger(tmp_path)
        assert led[("case-1", "bigo")]["status"] == "already"
        assert led[("case-1", "bigo")]["run_id"] == "2026-07-"

    def test_error_superseded_by_later_enriched(self, tmp_path):
        # 422 first, then a fixed run enriches -> current state is enriched.
        _write_events(tmp_path / "a.events.jsonl", [
            _ev("2026-07-20T10:00:00Z", "timeline", "case-9", "write", "error", "HTTP 422"),
        ])
        _write_events(tmp_path / "b.events.jsonl", [
            _ev("2026-07-21T10:00:00Z", "timeline", "case-9", "write", "enriched", "3 entries"),
        ])
        assert build_ledger(tmp_path)[("case-9", "timeline")]["status"] == "enriched"

    def test_intermediate_step_statuses_are_not_outcomes(self, tmp_path):
        # start/ok are step signals, not case-stage outcomes; the decisive
        # 'enriched' is the ledger entry, not the later-in-file 'ok' steps.
        _write_events(tmp_path / "a.events.jsonl", [
            _ev("2026-07-21T10:00:00Z", "timeline", "case-2", "start", "start"),
            _ev("2026-07-21T10:00:01Z", "timeline", "case-2", "source", "ok"),
            _ev("2026-07-21T10:00:02Z", "timeline", "case-2", "write", "enriched", "4 entries"),
            _ev("2026-07-21T10:00:03Z", "timeline", "case-2", "readback", "ok"),
        ])
        assert build_ledger(tmp_path)[("case-2", "timeline")]["status"] == "enriched"

    def test_case_with_no_outcome_event_is_absent(self, tmp_path):
        # A case that only got as far as 'start'/'ok' (e.g. the run crashed)
        # has no decisive outcome, so it is not in the ledger.
        _write_events(tmp_path / "a.events.jsonl", [
            _ev("2026-07-21T10:00:00Z", "bigo", "case-3", "start", "start"),
            _ev("2026-07-21T10:00:01Z", "bigo", "case-3", "prompt", "ok"),
        ])
        assert ("case-3", "bigo") not in build_ledger(tmp_path)

    def test_distinct_stages_tracked_separately(self, tmp_path):
        _write_events(tmp_path / "a.events.jsonl", [
            _ev("2026-07-21T10:00:00Z", "bigo", "case-4", "write", "enriched"),
            _ev("2026-07-21T10:00:01Z", "timeline", "case-4", "prereq", "unmet", "no source"),
        ])
        led = build_ledger(tmp_path)
        assert led[("case-4", "bigo")]["status"] == "enriched"
        assert led[("case-4", "timeline")]["status"] == "unmet"

    def test_outcome_statuses_are_the_runreport_set(self):
        assert set(OUTCOME_STATUSES) == {
            "enriched", "already", "unmet", "skipped", "error"}


class TestIterEvents:
    def test_tolerates_blank_and_malformed_lines(self, tmp_path):
        p = tmp_path / "a.events.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps(_ev("2026-07-21T10:00:00Z", "bigo", "c", "write", "enriched")) + "\n")
            f.write("\n")                       # blank
            f.write('{"partial": ')             # truncated trailing line
        evs = list(iter_events(tmp_path))
        assert len(evs) == 1
        assert evs[0]["slug"] == "c"

    def test_only_reads_events_jsonl_files(self, tmp_path):
        _write_events(tmp_path / "a.events.jsonl", [
            _ev("2026-07-21T10:00:00Z", "bigo", "c", "write", "enriched")])
        (tmp_path / "a-bigo.log").write_text("human log, not JSONL\n")
        (tmp_path / "notes.txt").write_text("ignore me\n")
        assert len(list(iter_events(tmp_path))) == 1


class TestSummaryAndWrite:
    def test_stage_summary_counts_per_stage(self, tmp_path):
        _write_events(tmp_path / "a.events.jsonl", [
            _ev("2026-07-21T10:00:00Z", "bigo", "c1", "write", "enriched"),
            _ev("2026-07-21T10:00:01Z", "bigo", "c2", "idempotency", "already"),
            _ev("2026-07-21T10:00:02Z", "bigo", "c3", "prereq", "unmet"),
            _ev("2026-07-21T10:00:03Z", "timeline", "c1", "write", "error"),
        ])
        summ = stage_summary(build_ledger(tmp_path))
        assert summ["bigo"] == {"enriched": 1, "already": 1, "unmet": 1}
        assert summ["timeline"] == {"error": 1}

    def test_write_ledger_roundtrips_utf8_sorted(self, tmp_path):
        rows = {
            ("case-z", "bigo"): {"slug": "case-z", "stage": "bigo",
                                 "status": "enriched", "ts": "2026-07-21T10:00:00Z",
                                 "run_id": "r1", "detail": "बिगो ५० लाख"},
            ("case-a", "bigo"): {"slug": "case-a", "stage": "bigo",
                                 "status": "already", "ts": "2026-07-21T10:00:01Z",
                                 "run_id": "r1", "detail": ""},
        }
        out = tmp_path / "ledger.jsonl"
        n = write_ledger(rows, out)
        assert n == 2
        lines = out.read_text(encoding="utf-8").splitlines()
        # sorted by (stage, slug) -> case-a before case-z
        assert json.loads(lines[0])["slug"] == "case-a"
        assert json.loads(lines[1])["detail"] == "बिगो ५० लाख"  # not \uXXXX-escaped
        assert "\\u" not in lines[1]


class TestCli:
    def test_main_writes_ledger_and_prints_summary(self, tmp_path, capsys):
        _write_events(tmp_path / "a.events.jsonl", [
            _ev("2026-07-21T10:00:00Z", "bigo", "c1", "write", "enriched"),
            _ev("2026-07-21T10:00:01Z", "bigo", "c2", "idempotency", "already"),
        ])
        out = tmp_path / "enrichment-ledger.jsonl"
        rc = main(["--log-dir", str(tmp_path), "--out", str(out)])
        assert rc == 0
        assert out.exists()
        assert len(out.read_text(encoding="utf-8").splitlines()) == 2
        printed = capsys.readouterr().out
        assert "bigo" in printed and "enriched" in printed

    def test_main_stage_filter_limits_summary(self, tmp_path, capsys):
        _write_events(tmp_path / "a.events.jsonl", [
            _ev("2026-07-21T10:00:00Z", "bigo", "c1", "write", "enriched"),
            _ev("2026-07-21T10:00:01Z", "timeline", "c1", "write", "error"),
        ])
        rc = main(["--log-dir", str(tmp_path), "--stage", "bigo", "--no-write"])
        assert rc == 0
        printed = capsys.readouterr().out
        assert "bigo" in printed
        assert "timeline" not in printed

    def test_main_status_filter_lists_matching_rows(self, tmp_path, capsys):
        # Audit use-case: "which cases errored?" -- --status lists the rows.
        _write_events(tmp_path / "a.events.jsonl", [
            _ev("2026-07-21T10:00:00Z", "timeline", "case-boom", "write", "error", "HTTP 422"),
            _ev("2026-07-21T10:00:01Z", "bigo", "case-ok", "write", "enriched"),
        ])
        rc = main(["--log-dir", str(tmp_path), "--status", "error", "--no-write"])
        assert rc == 0
        printed = capsys.readouterr().out
        assert "case-boom" in printed
        assert "case-ok" not in printed
