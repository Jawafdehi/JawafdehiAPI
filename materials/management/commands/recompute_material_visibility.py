"""Reconciler backstop for case-evidence material visibility.

The live path recomputes a material's ``visibility`` whenever a referring case is
written or deleted (``cases/signals.py``). This command is the periodic backstop
that heals any drift from a missed trigger — e.g. a crash between the case write
and its ``on_commit`` recompute, or a bulk operation that bypassed signals. Run it
on a schedule (mirrors the casework reaper) or by hand after a bulk data fix.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from materials.visibility import recompute_all


class Command(BaseCommand):
    help = (
        "Recompute the visibility of every case-referenced material and heal any "
        "drift from a missed live-recompute trigger."
    )

    def handle(self, *args, **options):
        changed = recompute_all()
        self.stdout.write(
            self.style.SUCCESS(
                f"material visibility reconciled: {changed} row(s) changed"
            )
        )
