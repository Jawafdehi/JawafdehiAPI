"""Register sweep orchestration (courts.scraper.sweep).

The sweep runs inside the shared ``court_scrape`` job, against a live government
portal, writing into a live corpus. So the behaviours pinned here are mostly the
brakes: stay inside the budget, say what was deferred, never turn an outage into
a wall of "this docket doesn't exist", and never rewrite a case that already exists.
"""

from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from courts.models import CaseEntity, Court, CourtCase, RegisterProbe
from courts.scraper.rows import ParsedEnrichment
from courts.scraper.sweep import ERROR_ABORT_THRESHOLD, run_sweep

NES_IRI = "https://jawafdehi.org/entity/person/ram-bahadur"


def _found():
    return ParsedEnrichment(
        core_fields={
            "registration_date_bs": "2076-11-13",
            "registration_date_ad": date(2020, 2, 25),
            "case_type": "नक्कली प्रमाण पत्र",
        },
        extra_data={"enrichment_hearings": []},
        entities=[{"side": "defendant", "name": "क", "address": None}],
    )


def _not_found():
    # What supreme/district/high actually return for an absent docket: empty
    # core/entities but a truthy extra_data.
    return ParsedEnrichment(
        core_fields={}, extra_data={"enrichment_hearings": [], "enrichment_timeline": []},
        entities=[],
    )


class _Spec:
    """A registry-shaped stand-in whose portal responses the test dictates."""

    def __init__(self, responses=None, default=None, raises=None):
        self.responses = responses or {}
        self.default = default
        self.raises = raises or set()
        self.calls = []

    def crawl_detail(self, fetch, court_id, case_number):
        self.calls.append(case_number)
        if case_number in self.raises or self.raises == "all":
            raise RuntimeError("portal blew up")
        return self.responses.get(case_number, self.default)


class _SweepTestCase(TestCase):
    databases = "__all__"

    def setUp(self):
        Court.objects.using("ngm").get_or_create(
            identifier="special", defaults={"court_type": "", "full_name_nepali": ""}
        )

    def _hold(self, *seqs):
        for s in seqs:
            CourtCase.objects.using("ngm").create(
                court_id="special", case_number=f"076-CR-{s:04d}"
            )

    def _sweep(self, spec, **kw):
        kw.setdefault("tail_probe", 0)
        kw.setdefault("delay", 0)
        kw.setdefault("sleep", lambda _s: None)
        return run_sweep(spec, fetch=None, court_id="special", **kw)


class TestSweepDiscovery(_SweepTestCase):
    def test_creates_the_missing_docket(self):
        self._hold(1, 3)  # 076-CR-0002 is the gap
        spec = _Spec(default=_found())
        stats = self._sweep(spec, write=True)
        assert spec.calls == ["076-CR-0002"]
        assert stats.created == 1
        assert CourtCase.objects.using("ngm").filter(case_number="076-CR-0002").exists()

    def test_dry_run_probes_but_writes_nothing(self):
        self._hold(1, 3)
        stats = self._sweep(_Spec(default=_found()), write=False)
        assert stats.probed == 1 and stats.created == 0
        assert not CourtCase.objects.using("ngm").filter(case_number="076-CR-0002").exists()

    def test_nothing_to_do_when_the_register_is_dense(self):
        self._hold(1, 2, 3)
        spec = _Spec(default=_found())
        assert self._sweep(spec, write=True).probed == 0
        assert spec.calls == []

    def test_a_tier_without_crawl_detail_is_skipped(self):
        self._hold(1, 3)
        assert self._sweep(object(), write=True).probed == 0


class TestNegativeCache(_SweepTestCase):
    def test_a_missing_docket_is_recorded_not_created(self):
        self._hold(1, 3)
        stats = self._sweep(_Spec(default=_not_found()), write=True)
        assert stats.missing == 1 and stats.created == 0
        assert not CourtCase.objects.using("ngm").filter(case_number="076-CR-0002").exists()
        probe = RegisterProbe.objects.using("ngm").get(case_number="076-CR-0002")
        assert probe.miss_count == 1

    def test_a_cached_absence_is_not_re_probed(self):
        self._hold(1, 3)
        self._sweep(_Spec(default=_not_found()), write=True)
        spec = _Spec(default=_not_found())
        assert self._sweep(spec, write=True).probed == 0
        assert spec.calls == [], "070-CR-0084-style never-issued numbers must not loop forever"

    def test_an_absence_is_re_probed_after_the_horizon(self):
        # "Missing" is provisional — a court can issue a number late.
        self._hold(1, 3)
        self._sweep(_Spec(default=_not_found()), write=True)
        RegisterProbe.objects.using("ngm").filter(case_number="076-CR-0002").update(
            last_probed_at=timezone.now() - timedelta(days=400)
        )
        spec = _Spec(default=_found())
        assert self._sweep(spec, write=True).created == 1
        assert spec.calls == ["076-CR-0002"]

    def test_a_re_check_never_displaces_work_never_done(self):
        """The rule that stops a large court livelocking.

        Filtering on the horizon alone is not enough: a court with more candidates
        than one horizon's worth of budget sees its earliest absences become
        eligible again before the list is exhausted, and they sort first — so every
        run re-walks the same prefix and the newest registers are never reached.
        Re-checks therefore go behind everything never probed.
        """
        self._hold(1, 30)  # 28 interior gaps
        self._sweep(_Spec(default=_not_found()), write=True, budget=5)
        RegisterProbe.objects.using("ngm").update(
            last_probed_at=timezone.now() - timedelta(days=400)  # all eligible again
        )
        spec = _Spec(default=_not_found())
        self._sweep(spec, write=True, budget=5)
        assert spec.calls == [f"076-CR-{s:04d}" for s in range(7, 12)], (
            "the second run must advance, not re-walk the first five"
        )

    def test_re_checks_run_once_the_new_work_is_exhausted(self):
        # Lower priority, not dropped: a court can issue a number late.
        self._hold(1, 4)  # 2 interior gaps
        self._sweep(_Spec(default=_not_found()), write=True)
        RegisterProbe.objects.using("ngm").update(
            last_probed_at=timezone.now() - timedelta(days=400)
        )
        spec = _Spec(default=_not_found())
        assert self._sweep(spec, write=True).probed == 2
        assert sorted(spec.calls) == ["076-CR-0002", "076-CR-0003"]

    def test_repeated_misses_accumulate(self):
        self._hold(1, 3)
        self._sweep(_Spec(default=_not_found()), write=True)
        RegisterProbe.objects.using("ngm").filter(case_number="076-CR-0002").update(
            last_probed_at=timezone.now() - timedelta(days=400)
        )
        self._sweep(_Spec(default=_not_found()), write=True)
        assert RegisterProbe.objects.using("ngm").get(case_number="076-CR-0002").miss_count == 2


class TestBudget(_SweepTestCase):
    def test_stops_at_the_budget_and_reports_the_remainder(self):
        self._hold(1, 10)  # 8 interior gaps
        stats = self._sweep(_Spec(default=_found()), write=True, budget=3)
        assert stats.probed == 3
        assert stats.deferred == 5, "a silent cap reads as full coverage"
        assert stats.probed + stats.deferred == 8

    def test_the_next_run_picks_up_where_it_left_off(self):
        self._hold(1, 10)
        self._sweep(_Spec(default=_found()), write=True, budget=3)
        stats = self._sweep(_Spec(default=_found()), write=True, budget=3)
        # The first three are now real cases, so they are no longer gaps.
        assert stats.probed == 3 and stats.deferred == 2

    def test_errors_spend_the_budget_too(self):
        """Budget is round-trips, not successes.

        Counting only successes lets a court that fails most probes issue many
        times the budgeted requests — 9-in-10 failures would spend 10x — inside a
        cron window shared with the cause-list crawl.
        """
        self._hold(1, 30)
        spec = _Spec(default=_found(), raises={f"076-CR-{s:04d}" for s in range(2, 30, 2)})
        stats = self._sweep(spec, write=True, budget=6)
        assert len(spec.calls) == 6
        assert stats.attempts == 6
        assert stats.probed + stats.errors == 6

    def test_every_candidate_is_accounted_for(self):
        # probed + errors + deferred == the candidate set, or a run that quietly
        # dropped work would look complete.
        self._hold(1, 30)
        spec = _Spec(default=_found(), raises={"076-CR-0003", "076-CR-0005"})
        stats = self._sweep(spec, write=True, budget=6)
        assert stats.probed + stats.errors + stats.deferred == 28

    def test_a_spent_budget_probes_nothing(self):
        self._hold(1, 10)
        spec = _Spec(default=_found())
        assert self._sweep(spec, write=True, budget=0).probed == 0
        assert spec.calls == []


class TestFailureHandling(_SweepTestCase):
    def test_an_error_is_never_recorded_as_a_missing_docket(self):
        # Otherwise one outage poisons the negative cache with real dockets.
        self._hold(1, 3)
        stats = self._sweep(_Spec(raises={"076-CR-0002"}), write=True)
        assert stats.errors == 1 and stats.missing == 0
        assert not RegisterProbe.objects.using("ngm").exists()

    def test_aborts_once_the_portal_looks_blocked(self):
        self._hold(1, 40)
        stats = self._sweep(_Spec(raises="all"), write=True, budget=100)
        assert stats.aborted is True
        assert stats.errors == ERROR_ABORT_THRESHOLD
        assert stats.deferred > 0

    def test_a_case_that_appears_mid_probe_is_counted_not_rewritten(self):
        """The race the ``existing`` counter exists for.

        Gap discovery and the write are seconds apart, and the cause-list crawl
        writes to the same table. If the sweep rewrote the case it found in that
        window it would call ``_replace_entities`` and drop the resolved nes_id.
        """
        self._hold(1, 3)

        class _RacingSpec(_Spec):
            def crawl_detail(self, fetch, court_id, case_number):
                # The cause list lands this docket while the probe is in flight.
                CourtCase.objects.using("ngm").create(
                    court_id=court_id, case_number=case_number, case_type="पुरानो"
                )
                CaseEntity.objects.using("ngm").create(
                    court_id=court_id, case_number=case_number,
                    side="defendant", name="क", nes_id=NES_IRI,
                )
                return super().crawl_detail(fetch, court_id, case_number)

        stats = self._sweep(_RacingSpec(default=_found()), write=True)
        assert stats.probed == 1 and stats.created == 0
        assert stats.existing == 1
        case = CourtCase.objects.using("ngm").get(case_number="076-CR-0002")
        assert case.case_type == "पुरानो", "the racing writer's row must survive"
        assert CaseEntity.objects.using("ngm").get(
            case_number="076-CR-0002"
        ).nes_id == NES_IRI
