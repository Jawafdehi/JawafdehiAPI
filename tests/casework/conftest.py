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
def _isolate_llm_response_cache(monkeypatch, tmp_path):
    """The `invoke_text` response cache (`casework/common/llm_cache.py`) is ON by
    default and defaults to `<repo>/work/llm-cache/`, so without this fixture the
    suite both litters that real directory and -- much worse -- READS from it.

    That second failure is the one to keep in mind: a test whose stub returns a
    fixed string writes an entry keyed on its own prompt, and the next run of any
    test using the same prompt gets a cache HIT, so its stub is never called and
    assertions like `assert seen_tiers == ["premium"]` fail against an empty list.
    The suite would pass or fail depending on what a previous run left on disk.
    Per-test tmp dir keeps every test's cache empty at start.

    A test that wants real default-directory behaviour delenv's this itself.
    """
    monkeypatch.setenv("CASEWORK_LLM_CACHE_DIR", str(tmp_path / "llm-cache"))
