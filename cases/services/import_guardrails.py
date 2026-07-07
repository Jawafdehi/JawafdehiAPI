"""Guard-rails for the CIAA import.

Kept dependency-light (no cloudpathlib / S3 stack, which the import command pulls
in behind an optional extra) so the ceiling logic is unit-testable in the minimal
CI environment.
"""

from __future__ import annotations

from django.core.management.base import CommandError


def enforce_truncation_ceiling(created: int, flagged: int, max_rate: float) -> None:
    """Abort when the roster-truncation guard fired on too large a share of cases.

    The guard should flag only a small fraction of a batch (historically ~0.4% of
    the Special-Court corpus). A spike means the ``समेत`` detector is over-matching
    — a regex or data-shape regression — so we raise ``CommandError`` and let the
    operator investigate rather than silently trust an import that mislabelled the
    corpus. No-op when nothing was created (dry-run / empty batch), which also
    avoids a divide-by-zero.
    """
    if created and (flagged / created) > max_rate:
        raise CommandError(
            f"Truncation-flag rate {flagged}/{created} "
            f"({flagged / created:.1%}) exceeds the --max-truncation-rate ceiling "
            f"({max_rate:.0%}). The roster-truncation detector is likely "
            "over-matching — investigate before trusting this import's flags."
        )
