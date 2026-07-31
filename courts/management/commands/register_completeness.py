"""Report how complete each court's registers are — from held rows alone, no network.

"Is the dataset complete?" does not require fetching anything. Court registers are
sequential per ``(year, series)``, so a slot the mirror doesn't hold between two
slots it does hold is a hole that is visible in the DB. The sweep exists to *close*
those holes; this command exists to *count* them, and it can run any time, on every
court, at zero cost to the courts' servers.

Three states, never two — the distinction is the whole point:

* **held** — the mirror has the case.
* **never issued** — probed, and the court doesn't have it either (``RegisterProbe``).
  Not missing data. Courts skip numbers; ``070-CR-0084`` is a real example.
* **unknown** — a hole nobody has asked the court about yet. It is *probably* a
  missing case, but calling it one before probing would overstate the problem.

So the honest completeness figure excludes never-issued slots from the denominator,
and the gap between "unknown" and "confirmed missing" only closes by sweeping. Where
a court HAS been swept, ``hit rate`` is the measured share of probed holes that
turned out to be real cases — on the special court that ran at 69 of 72 — which is
what makes the estimate an evidence-based projection rather than a guess.

Two blind spots, stated in the output rather than buried here:

* Only **interior** holes are visible. A register truncated at its tail reads 100%
  dense, because there is no held row past the truncation to imply the missing ones.
  Finding those needs the sweep's tail probe, i.e. the network.
* A ``(year, series)`` register with **zero** held rows is invisible entirely.

    manage.py register_completeness --court special
    manage.py register_completeness --format json > completeness.json
"""

import json

from django.core.management.base import BaseCommand, CommandError

from courts.models import CourtCase, RegisterProbe
from courts.scraper import registry
from courts.scraper.registers import (
    NGM_DB,
    compute_gaps,
    held_with_dates,
    sequence_confidence,
)


def court_completeness(court_id: str, *, using: str = NGM_DB) -> dict:
    """Register completeness for one court. Pure DB — no portal traffic."""
    rows = held_with_dates(court_id, using=using)
    held = [case_number for case_number, _ in rows]
    # tail_probe=0: the tail is only discoverable by asking the court, so counting
    # it here would invent a denominator this command cannot justify.
    holes = set(compute_gaps(held, tail_probe=0))
    never_issued = set(
        RegisterProbe.objects.using(using)
        .filter(court_id=court_id, case_number__in=holes)
        .values_list("case_number", flat=True)
    )
    unknown = holes - never_issued
    recovered = (
        CourtCase.objects.using(using)
        .filter(court_id=court_id, extra_data__source="register_sweep")
        .count()
    )
    probed = recovered + len(never_issued)
    issued = len(held) + len(unknown)
    return {
        "court": court_id,
        "held": len(held),
        "unknown": len(unknown),
        "never_issued": len(never_issued),
        "recovered_by_sweep": recovered,
        # Share of slots the register implies that the mirror actually holds.
        "density_pct": round(100.0 * len(held) / issued, 2) if issued else 100.0,
        # Of the holes anyone has asked about, how many were real cases. None until
        # a court has been swept — an unprobed court has no evidence either way.
        "hit_rate_pct": round(100.0 * recovered / probed, 1) if probed else None,
        "probed": probed,
        # Whether the numbering is a date-ordered counter at all. Everything above
        # is meaningless if it isn't — see registers.sequence_confidence.
        "sequence_confidence_pct": sequence_confidence(rows),
    }


class Command(BaseCommand):
    help = "Report register completeness per court from held rows alone (no network)."

    def add_arguments(self, parser):
        parser.add_argument("--court", default="all",
                            help="special | district | high | supreme | all")
        parser.add_argument("--court-id", default=None, help="a single leaf court")
        parser.add_argument("--format", choices=["table", "json"], default="table")

    def handle(self, *args, **o):
        try:
            tiers = registry.resolve(o["court"])
        except KeyError as exc:
            raise CommandError(str(exc)) from exc
        court_ids = (
            [o["court_id"]]
            if o["court_id"]
            else [cid for t in tiers for cid in registry.REGISTRY[t].court_ids(None)]
        )

        rows = [court_completeness(cid) for cid in court_ids]
        rows = [r for r in rows if r["held"]]
        rows.sort(key=lambda r: -r["unknown"])

        if o["format"] == "json":
            self.stdout.write(json.dumps({"courts": rows, "totals": _totals(rows)}, indent=2))
            return

        self.stdout.write(
            f"{'court':<18}{'held':>10}{'unknown':>10}{'never':>8}"
            f"{'dense%':>9}{'hit%':>7}{'seq%':>7}"
        )
        for r in rows:
            hit = "—" if r["hit_rate_pct"] is None else f"{r['hit_rate_pct']:.0f}"
            seq = "—" if r["sequence_confidence_pct"] is None else f"{r['sequence_confidence_pct']:.0f}"
            self.stdout.write(
                f"{r['court']:<18}{r['held']:>10,}{r['unknown']:>10,}"
                f"{r['never_issued']:>8,}{r['density_pct']:>9.2f}{hit:>7}{seq:>7}"
            )
        t = _totals(rows)
        self.stdout.write(
            f"\n{t['courts']} court(s): {t['held']:,} held, {t['unknown']:,} unknown holes, "
            f"{t['never_issued']:,} confirmed never issued — {t['density_pct']:.2f}% dense."
        )
        self.stdout.write(
            "Interior holes only: a register truncated at its tail reads 100% dense, and a "
            "(year, series) with no held rows is invisible. Both need the sweep to see."
        )
        self.stdout.write(
            "seq% is the check the rest rests on — the share of slots whose registration "
            "dates rise with the sequence. Real registers score ~100 at ANY density; treat "
            "a low score as 'this is not a counter', not as missing rows."
        )


def _totals(rows: list[dict]) -> dict:
    held = sum(r["held"] for r in rows)
    unknown = sum(r["unknown"] for r in rows)
    issued = held + unknown
    return {
        "courts": len(rows),
        "held": held,
        "unknown": unknown,
        "never_issued": sum(r["never_issued"] for r in rows),
        "recovered_by_sweep": sum(r["recovered_by_sweep"] for r in rows),
        "density_pct": round(100.0 * held / issued, 2) if issued else 100.0,
    }
