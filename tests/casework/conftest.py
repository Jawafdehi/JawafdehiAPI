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


@pytest.fixture(autouse=True)
def _isolate_review_files(monkeypatch, tmp_path):
    """Enricher `main()`s write a human review file on EVERY run, dry runs
    included (`casework/common/review.py`), defaulting to
    `<repo>/work/reviews/`. Same reason as the two fixtures above: without
    this, every test that drives `main()` end to end drops a Markdown file
    full of fixture Nepali into that real directory. A test that wants to read
    its own review file back finds it under this `tmp_path`.
    """
    monkeypatch.setenv("CASEWORK_REVIEW_DIR", str(tmp_path / "reviews"))
