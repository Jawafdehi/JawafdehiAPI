import pytest


@pytest.fixture(autouse=True)
def _isolate_casework_run_logs(monkeypatch, tmp_path):
    """Every enricher `main()` now calls `configure_run_logging(...)` (Task
    PP2), which defaults to writing run logs under
    `<repo>/work/enricher-runs/`. Without this autouse fixture, every test in
    this package that drives `main()` end to end would litter that real
    directory with a log + events file per test run. Point
    `CASEWORK_RUN_LOG_DIR` at a per-test tmp dir instead; a test that wants to
    inspect the produced files reads them back from this same `tmp_path`.

    A test that needs the real default-directory behavior (e.g.
    `test_cli.py::test_configure_run_logging_falls_back_to_repo_root_work_dir`)
    simply `monkeypatch.delenv("CASEWORK_RUN_LOG_DIR", ...)`s this back off
    for itself.
    """
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
