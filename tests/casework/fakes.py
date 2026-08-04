"""Shared fakes for the casework enricher tests.

A plain module rather than a `conftest.py` fixture: `FakeUsage` is installed as
an ATTRIBUTE on a fake `llm.usage` module (`fake_llm_usage.UsageAccumulator =
FakeUsage`), so the tests need the class object itself, not a fixture-injected
instance.
"""


class FakeUsage:
    """Stand-in for `llm.usage.UsageAccumulator` in the enricher `main()` tests.

    Each `_run_main` helper installs a fake `llm.usage` module via
    `monkeypatch.setitem(sys.modules, ...)` and needs a `UsageAccumulator` on
    it. Only two members are ever reached: `casework.common.run.finish_run`
    reads `.calls` to decide whether to render a usage table, and
    `render_usage_table` (itself stubbed out alongside this) would read
    `.as_dict()`. `calls` stays 0, so the table is always skipped.

    Was five byte-identical `_FakeUsage` copies, one per enricher test module.
    """

    def __init__(self):
        self.calls = 0

    def as_dict(self):
        return {"by_provider": []}
