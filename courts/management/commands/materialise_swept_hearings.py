"""Project stored ``enrichment_hearings`` JSON into the relational hearing table.

:func:`courts.scraper.base.materialise_detail_hearings` read the detail-page
hearing keys that ``special``/``high`` emit (``hearing_date``/``case_status``/
``decision_type``). ``supreme`` emits ``date``/``status``/``order_type``/``judges``
and ``district`` emits ``date``/``order``/``judge``, so for those two tiers every
hearing failed the date lookup and the function returned 0 — writing nothing,
raising nothing, logging nothing.

The consequence is corpus-scale and invisible: a swept supreme or district case
holds its full timeline in ``extra_data.enrichment_hearings`` and has no
``court_case_hearings`` rows at all, so every hearing-level query reads it as a
case the court never listed. Measured 2026-10-02 on supreme: 0 sweep-sourced
hearing rows against 97,817 swept cases, while the high courts — same function,
matching key names — hold 236,122. On the 3,239 Supreme corruption appeals it
drags hearing coverage down to 665 (21%), concentrated entirely in the recent
years where the sweep supplies most cases.

The sweep itself cannot repair this. ``upsert_from_detail`` returns ``False`` for
a case that already exists (re-enrichment drops resolved ``nes_id`` links via
``_replace_entities``), so a fixed materialiser never reaches the rows already
written.

This command does, and it needs **no network**: the hearings are already in the
database, just in the wrong shape. It re-reads each case's stored JSON through
the now-alias-aware materialiser.

    manage.py materialise_swept_hearings --court supreme           # dry run
    manage.py materialise_swept_hearings --court supreme --apply

Idempotent: the materialiser skips a date the case already holds, so a re-run
writes nothing and a part-finished run resumes safely. Additive only — it never
updates or deletes an existing hearing row, so the tiers that always worked
cannot regress.

~180k cases across supreme + district. The materialiser costs one ``exists()``
and at most one INSERT per stored hearing — no batching, deliberately, because
that per-date check is what makes the run resumable and additive. Budget for
roughly a million round trips on a full supreme + district pass, and use
``--limit`` to size a first run. Run it as a Job rather than ``kubectl exec`` —
a rollout SIGKILLs exec'd processes mid-run.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from courts.models import CourtCase
from courts.scraper import base, registry
from courts.scraper.rows import ParsedEnrichment

#: Cases whose stored JSON holds at least one hearing to project. An absent or
#: empty list means there is genuinely nothing to materialise — a freshly
#: registered case that has not reached a bench yet, which is most of the
#: FY2081–2082 tail.
HAS_STORED_HEARINGS = ~(
    Q(extra_data__enrichment_hearings=[])
    | Q(extra_data__enrichment_hearings__isnull=True)
)

#: Report cadence. A quiet 180k-row run is indistinguishable from a wedged one.
_PROGRESS_EVERY = 5_000


class Command(BaseCommand):
    help = "Write court_case_hearings rows from stored enrichment_hearings JSON."

    def add_arguments(self, parser):
        parser.add_argument(
            "--court",
            required=True,
            help="registry key: special | district | high | supreme | all",
        )
        parser.add_argument(
            "--court-id",
            default=None,
            help="restrict to one leaf court (default: every court in the tier)",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="actually write rows (default: dry run)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="cap the number of cases processed this run",
        )

    def handle(self, *args, **o):
        try:
            keys = registry.resolve(o["court"])
        except KeyError as exc:
            raise CommandError(str(exc)) from exc

        court_ids: list[str] = []
        for key in keys:
            court_ids.extend(registry.REGISTRY[key].court_ids(None))
        if o["court_id"]:
            if o["court_id"] not in court_ids:
                raise CommandError(
                    f"--court-id {o['court_id']} is not in tier(s) {', '.join(keys)}"
                )
            court_ids = [o["court_id"]]

        cases = (
            CourtCase.objects.using(base.NGM_DB)
            .filter(HAS_STORED_HEARINGS, court_id__in=court_ids)
            .order_by("court_id", "case_number")
            .values_list("court_id", "case_number", "extra_data")
        )
        if o["limit"]:
            cases = cases[: o["limit"]]

        if not o["apply"]:
            self._dry_run(cases)
            return

        seen = written = touched = 0
        for court_id, case_number, extra_data in cases.iterator(chunk_size=2_000):
            seen += 1
            rows = base.materialise_detail_hearings(
                court_id,
                case_number,
                ParsedEnrichment(extra_data=extra_data or {}),
            )
            if rows:
                written += rows
                touched += 1
            if seen % _PROGRESS_EVERY == 0:
                self.stdout.write(f"  {seen} cases … {written} hearing rows written")
        self.stdout.write(
            self.style.SUCCESS(
                f"{seen} cases scanned, {touched} gained hearings, "
                f"{written} rows written."
            )
        )

    def _dry_run(self, cases) -> None:
        """Count what --apply would write, without touching the database.

        Deliberately does NOT run the materialiser's per-date ``exists()`` check:
        that is one query per hearing, and on a 180k-case tier a dry run would
        cost more than the real thing. So ``writable`` is an UPPER bound — it
        counts every stored hearing carrying a recognised date key, including any
        the case already holds. On a tier that has never materialised (supreme,
        district) the two are the same number; on one that has, expect the real
        run to write fewer.
        """
        seen = writable = unrecognised = 0
        for _court_id, _case_number, extra_data in cases.iterator(chunk_size=2_000):
            seen += 1
            for hearing in (extra_data or {}).get("enrichment_hearings") or []:
                if base.recognised_hearing_date(hearing):
                    writable += 1
                else:
                    unrecognised += 1
            if seen % _PROGRESS_EVERY == 0:
                self.stdout.write(f"  {seen} cases … {writable} hearings so far")
        self.stdout.write(
            f"{seen} cases hold stored hearings; up to {writable} rows would be "
            f"written ({unrecognised} entries carry no recognised date key)."
        )
        if unrecognised:
            self.stdout.write(
                self.style.WARNING(
                    "entries with no recognised date key mean a parser emits a "
                    "shape the alias tuples in courts/scraper/base.py do not "
                    "cover — add it there before applying."
                )
            )
        self.stdout.write(self.style.WARNING("dry run — pass --apply to write."))
