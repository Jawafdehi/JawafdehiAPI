"""Network-free completeness reporting.

The point of this command is that it can answer "how complete is the mirror?"
without asking a single court anything. The tests therefore pin the accounting —
especially the three-state split, since collapsing "nobody asked" into "missing"
would overstate the problem and collapsing it into "fine" would hide it.
"""

import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from courts.models import Court, CourtCase, RegisterProbe
from courts.management.commands.register_completeness import court_completeness


class RegisterCompletenessTests(TestCase):
    databases = "__all__"

    def setUp(self):
        Court.objects.using("ngm").get_or_create(
            identifier="special", defaults={"court_type": "", "full_name_nepali": ""}
        )

    def _hold(self, *seqs, **kw):
        for s in seqs:
            CourtCase.objects.using("ngm").create(
                court_id="special", case_number=f"076-CR-{s:04d}", **kw
            )

    def test_counts_the_holes_a_register_implies(self):
        self._hold(1, 2, 5)  # 0003 and 0004 are holes
        r = court_completeness("special")
        assert r["held"] == 3
        assert r["unknown"] == 2
        assert r["density_pct"] == 60.0

    def test_a_dense_register_is_100_percent(self):
        self._hold(1, 2, 3)
        assert court_completeness("special")["density_pct"] == 100.0

    def test_a_confirmed_never_issued_slot_is_not_counted_against_us(self):
        """The distinction the whole report rests on.

        Courts skip numbers — 070-CR-0084 is absent from the court's own portal.
        Counting a slot the court never issued as missing data would permanently
        cap completeness below 100% for reasons that have nothing to do with us.
        """
        self._hold(1, 2, 5)
        RegisterProbe.objects.using("ngm").create(
            court_id="special", case_number="076-CR-0003"
        )
        r = court_completeness("special")
        assert r["never_issued"] == 1
        assert r["unknown"] == 1
        assert r["density_pct"] == 75.0, "3 held of 4 issued, not of 5 slots"

    def test_an_unprobed_hole_is_unknown_not_missing(self):
        # Nothing has asked the court about these, so the report must not present
        # them as confirmed gaps — only as unresolved ones.
        self._hold(1, 5)
        r = court_completeness("special")
        assert r["unknown"] == 3 and r["never_issued"] == 0
        assert r["hit_rate_pct"] is None, "no evidence either way until something is probed"

    def test_hit_rate_is_measured_not_assumed(self):
        # 2 slots recovered by sweeping, 1 confirmed never issued -> 67%.
        self._hold(1, 2, 9)
        self._hold(3, 4, extra_data={"source": "register_sweep"})
        RegisterProbe.objects.using("ngm").create(
            court_id="special", case_number="076-CR-0005"
        )
        r = court_completeness("special")
        assert r["recovered_by_sweep"] == 2
        assert r["probed"] == 3
        assert r["hit_rate_pct"] == 66.7

    def test_the_tail_is_not_counted(self):
        # A register truncated past its high-water mark is invisible from the DB.
        # Claiming otherwise would need a denominator this command can't justify.
        self._hold(1, 2, 3)
        assert court_completeness("special")["unknown"] == 0

    def test_json_output_carries_totals(self):
        self._hold(1, 5)
        out = StringIO()
        call_command("register_completeness", "--court-id", "special",
                     "--format", "json", stdout=out)
        payload = json.loads(out.getvalue())
        assert payload["totals"]["unknown"] == 3
        assert payload["courts"][0]["court"] == "special"

    def test_sequence_confidence_validates_the_whole_metric(self):
        """Density only means anything if the numbering is a date-ordered counter.

        Measured on the live mirror this separates cleanly: real registers score
        99.4–100% whether they are 99% dense (special 076-CR) or 0.2% dense
        (kathmandudc 073-PC). So a low score means "not a sequence", not "rows
        missing" — the two would otherwise be indistinguishable.
        """
        for seq, date_bs in [(1, "2076-04-01"), (2, "2076-04-05"), (5, "2076-05-01")]:
            CourtCase.objects.using("ngm").create(
                court_id="special", case_number=f"076-CR-{seq:04d}",
                registration_date_bs=date_bs,
            )
        assert court_completeness("special")["sequence_confidence_pct"] == 100.0

    def test_numbering_that_is_not_a_counter_scores_low(self):
        # Dates jumping around against the sequence = an opaque id, and the holes
        # it implies are fiction.
        for seq, date_bs in [(1, "2076-09-01"), (2, "2076-04-01"), (3, "2076-12-01"),
                             (4, "2076-05-01"), (5, "2076-01-01")]:
            CourtCase.objects.using("ngm").create(
                court_id="special", case_number=f"076-CR-{seq:04d}",
                registration_date_bs=date_bs,
            )
        assert court_completeness("special")["sequence_confidence_pct"] < 60

    def test_undated_rows_do_not_fake_confidence(self):
        self._hold(1, 2, 3)  # no registration_date_bs
        assert court_completeness("special")["sequence_confidence_pct"] is None

    def test_table_output_names_its_blind_spots(self):
        self._hold(1, 5)
        out = StringIO()
        call_command("register_completeness", "--court-id", "special", stdout=out)
        text = out.getvalue()
        assert "unknown holes" in text
        assert "tail reads 100% dense" in text, "a caveat only in the docstring is a caveat nobody reads"
