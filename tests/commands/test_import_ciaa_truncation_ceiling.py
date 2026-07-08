"""Circuit-breaker on the roster-truncation guard.

The guard should fire on a small fraction of cases (historically ~0.4% of the
Special-Court corpus). If it fires on an anomalously large share, the ``समेत``
heuristic has regressed and is over-matching — the import must abort loudly
rather than silently mislabel the corpus. The ceiling lives in a dependency-light
helper so it is unit-testable without the command's optional S3 stack.
"""

import pytest
from django.core.management.base import CommandError

from cases.services.import_guardrails import enforce_truncation_ceiling


def test_aborts_when_rate_exceeds_ceiling():
    # 60 of 100 = 60% >> the 10% default ceiling.
    with pytest.raises(CommandError, match="exceeds the --max-truncation-rate"):
        enforce_truncation_ceiling(created=100, flagged=60, max_rate=0.10)


def test_ok_when_rate_within_ceiling():
    # 2 of 100 = 2% < 10% (mirrors the ~0.4% corpus baseline) → no raise.
    enforce_truncation_ceiling(created=100, flagged=2, max_rate=0.10)


def test_ok_at_the_boundary():
    # Exactly at the ceiling is allowed (strictly-greater trips it).
    enforce_truncation_ceiling(created=100, flagged=10, max_rate=0.10)


def test_custom_ceiling_is_honoured():
    with pytest.raises(CommandError, match="exceeds the --max-truncation-rate"):
        enforce_truncation_ceiling(created=100, flagged=3, max_rate=0.01)


def test_no_division_by_zero_when_nothing_created():
    # Dry-run / empty batch: 0 created must not raise or divide by zero.
    enforce_truncation_ceiling(created=0, flagged=0, max_rate=0.10)
