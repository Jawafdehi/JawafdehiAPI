"""Register-gap discovery (courts.scraper.registers).

Pure — no DB, no network. The cases here are the number shapes that actually
occur in the live mirror; each one broke a naive implementation during the
completeness audit, so they are regression tests, not hypotheticals.
"""

from courts.scraper.registers import (
    RegisterKey,
    compute_gaps,
    format_case_number,
    parse_case_number,
)


def _numbers(year, series, seqs, pad=4):
    return [f"{year}-{series}-{s:0{pad}d}" for s in seqs]


class TestParseCaseNumber:
    def test_modern_shape(self):
        p = parse_case_number("076-CR-0294")
        assert p is not None
        assert p.key == RegisterKey(year="076", series="CR")
        assert p.seq == 294
        assert p.pad == 4

    def test_numeric_series_is_a_series_not_a_year(self):
        # District registers use numeric series (080-02-, 080-07-). Treating
        # "the non-numeric token" as the series drops these entirely.
        p = parse_case_number("080-02-3073")
        assert p is not None
        assert p.key == RegisterKey(year="080", series="02")
        assert p.seq == 3073

    def test_sequence_wider_than_the_usual_pad(self):
        # kathmandudc 080-C1 holds >10k rows; a 4-digit assumption truncates.
        p = parse_case_number("080-C1-10961")
        assert p is not None
        assert p.seq == 10961
        assert p.pad == 5

    def test_legacy_scheme_is_not_enumerable(self):
        # 93-068-0128: position 2 is the YEAR, not a series. Walking it as a
        # sequence would fabricate numbers that never existed.
        assert parse_case_number("93-068-0128") is None

    def test_junk_is_not_enumerable(self):
        for bad in ["", "   ", "nonsense", "076-CR", "076-CR-", "0076-CR-0001"]:
            assert parse_case_number(bad) is None, bad


class TestFormatCaseNumber:
    def test_pads_to_width(self):
        assert format_case_number(RegisterKey("076", "CR"), 294, 4) == "076-CR-0294"

    def test_widens_rather_than_truncates(self):
        # A sequence that outgrew the register's pad must not be cut down.
        assert format_case_number(RegisterKey("080", "C1"), 10961, 4) == "080-C1-10961"

    def test_round_trips_through_parse(self):
        n = format_case_number(RegisterKey("082", "OA"), 7, 4)
        p = parse_case_number(n)
        assert p.seq == 7 and p.key.series == "OA"


class TestComputeGaps:
    def test_finds_an_interior_hole(self):
        held = _numbers("076", "CR", [292, 293, 295, 296])
        gaps = compute_gaps(held, tail_probe=0)
        assert "076-CR-0294" in gaps

    def test_finds_holes_from_slot_one(self):
        # A register whose opening slots were never listed is still a gap.
        held = _numbers("073", "CR", [3, 4])
        assert compute_gaps(held, tail_probe=0) == ["073-CR-0001", "073-CR-0002"]

    def test_survives_a_long_consecutive_run(self):
        # Regression: 082-CR-0162..0170 is a genuine 9-slot run. A
        # "stop after K consecutive misses" walk with K<=9 would declare the
        # register finished at 161 and silently drop everything after it.
        held = _numbers("082", "CR", list(range(1, 162)) + [171, 179])
        gaps = compute_gaps(held, tail_probe=0)
        for seq in range(162, 171):
            assert f"082-CR-{seq:04d}" in gaps
        assert "082-CR-0178" in gaps  # between 171 and the 179 high-water mark

    def test_tail_probe_extends_past_the_high_water_mark(self):
        # Density alone only sees interior holes; a truncated register reads as
        # complete. The tail probe is what finds the missing end.
        gaps = compute_gaps(_numbers("082", "CR", [1, 2, 3]), tail_probe=5)
        assert gaps == [f"082-CR-{s:04d}" for s in range(4, 9)]

    def test_tail_probe_can_be_disabled(self):
        assert compute_gaps(_numbers("082", "CR", [1, 2, 3]), tail_probe=0) == []

    def test_pad_width_comes_from_the_register_not_a_constant(self):
        held = ["080-C1-10001", "080-C1-10003"]
        gaps = compute_gaps(held, tail_probe=1)
        assert "080-C1-10002" in gaps
        assert "080-C1-10004" in gaps

    def test_modal_pad_wins_over_an_outlier(self):
        # One oddly-written row must not re-pad the whole register.
        held = ["076-CR-0001", "076-CR-0002", "076-CR-4"]
        gaps = compute_gaps(held, tail_probe=1)
        assert "076-CR-0003" in gaps
        assert "076-CR-0005" in gaps

    def test_registers_are_independent(self):
        held = _numbers("076", "CR", [1, 3]) + _numbers("076", "OA", [1, 2])
        gaps = compute_gaps(held, tail_probe=0)
        assert gaps == ["076-CR-0002"]

    def test_years_are_independent(self):
        held = _numbers("076", "CR", [5]) + _numbers("077", "CR", [1])
        gaps = compute_gaps(held, tail_probe=0)
        assert all(g.startswith("076-CR-") for g in gaps)
        assert "077-CR-0001" not in gaps

    def test_legacy_numbers_contribute_nothing(self):
        # Legacy rows must neither create a register nor perturb a real one.
        held = ["93-068-0128", "93-068-0141"] + _numbers("076", "CR", [1, 3])
        assert compute_gaps(held, tail_probe=0) == ["076-CR-0002"]

    def test_finds_a_single_never_issued_slot(self):
        # 070-CR-0084 is absent from the court's own portal too. Discovery still
        # has to surface it — only the sweep's negative cache can know it is
        # settled, and only after asking.
        held = _numbers("070", "CR", list(range(1, 84)) + [85])
        assert compute_gaps(held, tail_probe=0) == ["070-CR-0084"]

    def test_empty_input(self):
        assert compute_gaps([], tail_probe=25) == []

    def test_only_legacy_input_yields_nothing(self):
        assert compute_gaps(["93-068-0128"], tail_probe=25) == []

    def test_output_is_unique(self):
        held = _numbers("076", "CR", [1, 5]) + _numbers("075", "OA", [2])
        gaps = compute_gaps(held, tail_probe=2)
        assert len(gaps) == len(set(gaps))


class TestProbePriority:
    """Order is load-bearing: a run is budgeted, so it only ever reaches a prefix.

    kathmandudc alone implies ~84,000 candidates across 147 registers against a
    default budget of 200. Lexical order would spend every run on the oldest
    registers and never reach the years anyone is asking about.
    """

    def test_newest_register_year_comes_first(self):
        held = _numbers("076", "CR", [1, 3]) + _numbers("082", "CR", [1, 3])
        gaps = compute_gaps(held, tail_probe=0)
        assert gaps == ["082-CR-0002", "076-CR-0002"]

    def test_the_growing_tail_outranks_interior_holes(self):
        # The tail is where a live register adds dockets; an interior hole is
        # static. Both matter, but only one is still moving.
        held = _numbers("082", "CR", [1, 3])
        gaps = compute_gaps(held, tail_probe=2)
        assert gaps == ["082-CR-0004", "082-CR-0005", "082-CR-0002"]

    def test_sequence_order_within_a_group_is_deterministic(self):
        held = _numbers("082", "CR", [1, 5])
        assert compute_gaps(held, tail_probe=0) == [
            "082-CR-0002", "082-CR-0003", "082-CR-0004",
        ]

    def test_duplicate_held_numbers_are_harmless(self):
        held = _numbers("076", "CR", [1, 1, 1, 3])
        assert compute_gaps(held, tail_probe=0) == ["076-CR-0002"]


class TestSeriesFilter:
    """A court's series are not equal in value or cost.

    On the special court the 72 ``-CR-`` holes are corruption prosecutions; the
    575 ``-OA-`` holes are mostly procedural filings or numbers never issued.
    Sweeping one without the other is the difference between 4 minutes and an hour.
    """

    def test_restricts_the_walk_to_named_registers(self):
        held = _numbers("076", "CR", [1, 3]) + _numbers("076", "OA", [1, 3])
        assert compute_gaps(held, tail_probe=0, series={"CR"}) == ["076-CR-0002"]

    def test_accepts_several_series(self):
        held = _numbers("076", "CR", [1, 3]) + _numbers("076", "OA", [1, 3]) + _numbers("076", "WO", [1, 3])
        gaps = compute_gaps(held, tail_probe=0, series={"CR", "OA"})
        assert sorted(gaps) == ["076-CR-0002", "076-OA-0002"]

    def test_is_case_insensitive(self):
        held = _numbers("076", "CR", [1, 3])
        assert compute_gaps(held, tail_probe=0, series={"cr"}) == ["076-CR-0002"]

    def test_no_filter_means_every_series(self):
        held = _numbers("076", "CR", [1, 3]) + _numbers("076", "OA", [1, 3])
        assert len(compute_gaps(held, tail_probe=0)) == 2

    def test_an_unknown_series_yields_nothing_rather_than_everything(self):
        # A typo must not silently sweep the whole court.
        held = _numbers("076", "CR", [1, 3])
        assert compute_gaps(held, tail_probe=0, series={"NOPE"}) == []

    def test_the_excluded_series_neither_leaks_nor_shifts_the_pad(self):
        # Each register keeps its own pad width; filtering must not borrow the
        # other series' width, and must not emit any of its numbers.
        held = ["076-CR-0001", "076-CR-0003", "076-C1-10001", "076-C1-10003"]
        gaps = compute_gaps(held, tail_probe=0, series={"C1"})
        assert "076-C1-10002" in gaps
        assert not [g for g in gaps if g.startswith("076-CR")]
