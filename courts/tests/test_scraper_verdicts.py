"""Verdict recovery from court orders.

The load-bearing risk here is not a crash, it is a WRONG verdict written
silently into a field that feeds a published conviction rate. So the parse layer
refuses anything it cannot vouch for, and these tests pin that refusal.
"""

import json
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from courts.management.commands import extract_verdicts
from courts.management.commands.extract_verdicts import Command
from courts.models import Court, CourtCase, CourtCaseHearing
from courts.scraper.verdicts import (
    ACQUITTAL,
    FULL,
    MAX_CHARS,
    PROVENANCE_KEY,
    backlog,
    build_hearing,
    build_prompt,
    court_coded_verdicts,
    derived_hearing_filter,
    is_decided,
    order_urls,
    parse_response,
)


def _resp(**over):
    body = {
        "decision_type": FULL,
        "judges": "श्री क\nश्री ख",
        "verdict_date_bs": "2076-12-03",
        "evidence": "जरिवाना हुने ठहर्छ",
        "confidence": "high",
    }
    body.update(over)
    return json.dumps(body, ensure_ascii=False)


class ParseResponseTests(TestCase):
    def test_a_clean_answer_parses(self):
        ex = parse_response(_resp())
        self.assertEqual(ex.decision_type, FULL)
        self.assertFalse(ex.abstained)
        self.assertEqual(ex.confidence, "high")

    def test_abstain_is_an_answer_not_an_error(self):
        """Abstention is the correct output for an unreadable judgment."""
        ex = parse_response(_resp(decision_type="ABSTAIN"))
        self.assertTrue(ex.abstained)
        self.assertIsNone(ex.decision_type)

    def test_a_decision_outside_the_closed_set_is_rejected(self):
        """The court has exactly three dispositions. A fourth is a parse failure,
        never a new category -- silently storing it would corrupt every rate."""
        for bogus in ("guilty", "ठहर्छ", "आंशिक", "", "convicted"):
            with self.assertRaises(ValueError):
                parse_response(_resp(decision_type=bogus))

    def test_prose_around_the_json_is_tolerated(self):
        ex = parse_response("Here is the result:\n```json\n" + _resp() + "\n```\nDone.")
        self.assertEqual(ex.decision_type, FULL)

    def test_malformed_and_empty_responses_raise(self):
        for bad in ("", "   ", "not json at all", "{oops", "[]"):
            with self.assertRaises(ValueError):
                parse_response(bad)

    def test_a_stray_date_format_is_dropped_not_fatal(self):
        """case_status is the authoritative date, so a bad model date is noise."""
        ex = parse_response(_resp(verdict_date_bs="03/12/2076"))
        self.assertIsNone(ex.verdict_date_bs)
        self.assertEqual(ex.decision_type, FULL)

    def test_long_fields_are_capped(self):
        ex = parse_response(_resp(evidence="क" * 5000, judges="ख" * 5000))
        self.assertLessEqual(len(ex.evidence), 300)
        self.assertLessEqual(len(ex.judges), 500)


class BuildPromptTests(TestCase):
    def test_a_short_judgment_is_sent_whole(self):
        self.assertIn("ठहर्छ", build_prompt("क ख ठहर्छ", "076-CR-0001"))

    def test_a_long_judgment_keeps_its_TAIL(self):
        """The holding sits at the END. Plain truncation would cut off exactly
        the section the answer depends on and invite a confident wrong guess."""
        text = "HEAD" + ("पदपद " * 40_000) + "OPERATIVE ठहर्छ"
        out = build_prompt(text, "076-CR-0001")
        self.assertIn("HEAD", out)
        self.assertIn("OPERATIVE ठहर्छ", out)
        self.assertLess(len(out), MAX_CHARS + 500)


class BuildHearingTests(TestCase):
    databases = "__all__"

    def setUp(self):
        self.court = Court.objects.create(identifier="special", court_type="special", full_name_nepali="विशेष अदालत")
        self.case = CourtCase.objects.create(
            case_number="076-CR-0215", court=self.court,
            case_status="फैसला (मिती: २०७६/१२/०३)",
            extra_data={"court_orders": ["https://s3.example/x.docx"]},
        )
        self.ex = parse_response(_resp())

    def test_the_row_carries_the_courts_date_not_the_models(self):
        """Only the disposition is model-derived; the date is the court's own."""
        ex = parse_response(_resp(verdict_date_bs="2000-01-01"))
        h = build_hearing(self.case, ex, order_url="u", model="m", now=timezone.now())
        self.assertEqual(h.hearing_date_bs, "2076-12-03")
        self.assertIsNotNone(h.hearing_date_ad)
        self.assertEqual(h.extra_data[PROVENANCE_KEY]["model_verdict_date_bs"], "2000-01-01")

    def test_every_written_row_is_marked_model_derived(self):
        """A published conviction rate mixing scraped and inferred verdicts
        without saying so would misrepresent the data."""
        h = build_hearing(self.case, self.ex, order_url="u", model="m", now=timezone.now())
        prov = h.extra_data[PROVENANCE_KEY]
        self.assertTrue(prov["derived"])
        self.assertEqual(prov["model"], "m")
        self.assertEqual(prov["order_url"], "u")
        self.assertEqual(prov["evidence"], "जरिवाना हुने ठहर्छ")

        h.save(using="ngm")
        self.assertTrue(CourtCaseHearing.objects.using("ngm").filter(derived_hearing_filter()).exists())
        scraped = CourtCaseHearing.objects.using("ngm").create(
            case_number="076-CR-0999", court=self.court, hearing_date_bs="2076-01-01",
            hearing_date_ad="2019-04-14", decision_type=FULL, scraped_at=timezone.now(),
        )
        found = CourtCaseHearing.objects.using("ngm").filter(derived_hearing_filter())
        self.assertNotIn(scraped.pk, [h.pk for h in found])

    def test_an_abstention_can_never_become_a_row(self):
        with self.assertRaises(ValueError):
            build_hearing(self.case, parse_response(_resp(decision_type="ABSTAIN")),
                          order_url="u", model="m", now=timezone.now())

    def test_a_case_without_a_verdict_date_is_refused(self):
        """No date means no defensible hearing_date -- better to skip the case
        than to invent one or reach for a sentinel."""
        self.case.case_status = "फैसला"
        with self.assertRaises(ValueError):
            build_hearing(self.case, self.ex, order_url="u", model="m", now=timezone.now())


class BacklogTests(TestCase):
    databases = "__all__"

    def setUp(self):
        self.court = Court.objects.create(identifier="special", court_type="special", full_name_nepali="विशेष अदालत")
        self.other = Court.objects.create(identifier="supreme", court_type="supreme", full_name_nepali="सर्वोच्च अदालत")

    def _case(self, num, *, orders=True, status="फैसला (मिती: २०७६/१२/०३)", court=None):
        return CourtCase.objects.create(
            case_number=num, court=court or self.court, case_status=status,
            extra_data={"court_orders": ["https://s3.example/x.docx"]} if orders else {},
        )

    def test_it_selects_only_cases_that_need_and_can_have_a_verdict(self):
        want = self._case("076-CR-0001")
        self._case("076-CR-0002", orders=False)          # nothing to read
        self._case("076-CR-0003", status="")             # no status at all
        already = self._case("076-CR-0004")
        CourtCaseHearing.objects.create(
            case_number=already.case_number, court=self.court, hearing_date_bs="2076-01-01",
            hearing_date_ad="2019-04-14", decision_type=ACQUITTAL, scraped_at=timezone.now(),
        )
        self.assertEqual([c.case_number for c in backlog()], [want.case_number])

    def test_a_hearing_without_a_disposition_does_not_count_as_decided(self):
        """The backfilled cases have exactly this shape -- hearing rows present,
        decision_type never populated. Missing them would defeat the point."""
        c = self._case("076-CR-0005")
        CourtCaseHearing.objects.create(
            case_number=c.case_number, court=self.court, hearing_date_bs="2076-01-01",
            hearing_date_ad="2019-04-14", decision_type=None, scraped_at=timezone.now(),
        )
        self.assertIn(c.case_number, [x.case_number for x in backlog()])

    def test_it_stays_within_one_court(self):
        self._case("076-CR-0007", court=self.other)
        self.assertEqual(list(backlog(court_identifier="special")), [])

    def test_a_named_case_bypasses_the_filters_for_smoke_testing(self):
        c = self._case("076-CR-0008", orders=False)
        self.assertEqual([x.case_number for x in backlog(case_number=c.case_number)], [c.case_number])


class CaseHelperTests(TestCase):
    databases = "__all__"

    def setUp(self):
        self.court = Court.objects.create(identifier="special", court_type="special", full_name_nepali="विशेष अदालत")

    def test_is_decided_reads_the_shared_parser(self):
        mk = lambda s: CourtCase(case_number="x", court=self.court, case_status=s)  # noqa: E731
        self.assertTrue(is_decided(mk("फैसला (मिती: २०७६/१२/०३)")))
        self.assertFalse(is_decided(mk("चलिरहेको")))
        self.assertFalse(is_decided(mk("")))

    def test_order_urls_ignores_junk_entries(self):
        c = CourtCase(case_number="x", court=self.court,
                      extra_data={"court_orders": ["https://a/x.doc", "", None, 7, "not-a-url"]})
        self.assertEqual(order_urls(c), ["https://a/x.doc"])
        self.assertEqual(order_urls(CourtCase(case_number="y", court=self.court, extra_data=None)), [])

    def test_order_urls_reads_the_CANONICAL_document_source_shape(self):
        c = CourtCase(case_number="x", court=self.court, document_sources=[{
            "source_type": "COURT_ORDER", "document_id": "d",
            "url": [{"link": "https://a/alt.pdf", "role": "ALTERNATE"},
                    {"link": "https://a/raw.doc", "role": "RAW"}],
        }])
        # RAW first: it is the judgment; ALTERNATE may be a scan or a summary.
        self.assertEqual(order_urls(c), ["https://a/raw.doc", "https://a/alt.pdf"])

    def test_order_urls_reads_the_LEGACY_HYBRID_shape(self):
        """The 2026-07 rehost left historical cases with a SCALAR `url` plus a
        `links` list. materials.jsonld drops these, so reading document_sources
        naively would make every historical order invisible."""
        c = CourtCase(case_number="x", court=self.court, document_sources=[{
            "source_type": "COURT_ORDER",
            "url": "https://a/raw.doc",
            "links": [{"link": "https://a/raw.doc", "role": "RAW"}],
        }])
        self.assertEqual(order_urls(c), ["https://a/raw.doc"])

    def test_order_urls_skips_relative_legacy_court_orders_keys(self):
        """Historical `extra_data['court_orders']` holds STORAGE KEYS, not URLs.
        Treating one as a URL would fetch nothing and look like a missing order."""
        c = CourtCase(case_number="x", court=self.court,
                      extra_data={"court_orders": ["court-orders/special/076-CR-0006.1.doc"]})
        self.assertEqual(order_urls(c), [])

    def test_order_urls_ignores_non_order_document_sources(self):
        c = CourtCase(case_number="x", court=self.court, document_sources=[
            {"source_type": "PRESS_RELEASE", "url": [{"link": "https://a/pr.pdf", "role": "RAW"}]},
        ])
        self.assertEqual(order_urls(c), [])


class EvalAnswerKeyTests(TestCase):
    """The eval is the only evidence the written verdicts are trustworthy, so it
    must never grade the model against the model."""

    databases = "__all__"

    def setUp(self):
        self.court = Court.objects.create(identifier="special", court_type="special", full_name_nepali="विशेष अदालत")

    def _hearing(self, num, *, derived, decision=FULL):
        return CourtCaseHearing.objects.create(
            case_number=num, court=self.court, hearing_date_bs="2076-01-01",
            hearing_date_ad="2019-04-14", decision_type=decision, scraped_at=timezone.now(),
            extra_data={PROVENANCE_KEY: {"derived": True}} if derived else {},
        )

    def test_the_answer_key_holds_only_what_the_court_coded(self):
        self._hearing("076-CR-0001", derived=False)
        self._hearing("076-CR-0002", derived=True)
        self.assertEqual(dict(court_coded_verdicts("special")), {"076-CR-0001": FULL})

    def test_a_written_verdict_never_becomes_its_own_ground_truth(self):
        """Left unfiltered, each --write run would enlarge the key with the
        model's own answers and accuracy would drift towards self-agreement."""
        case = CourtCase.objects.create(
            case_number="076-CR-0215", court=self.court, case_status="फैसला (मिती: २०७६/१२/०३)",
        )
        build_hearing(case, parse_response(_resp()), order_url="u", model="m", now=timezone.now()).save()
        self.assertEqual(dict(court_coded_verdicts("special")), {})

    def test_hearings_without_a_disposition_are_not_an_answer(self):
        self._hearing("076-CR-0003", derived=False, decision=None)
        self.assertEqual(dict(court_coded_verdicts("special")), {})


class PolitenessGapTests(TestCase):
    """A dry run downloads exactly as much as a write run, and a run of failures
    downloads more per minute than a run of successes. The gap has to sit on the
    DOWNLOAD, not on the write and not at the end of the loop."""

    def test_the_gap_precedes_every_download_but_the_first(self):
        cmd = Command()
        cmd._downloads = 0
        with mock.patch.object(extract_verdicts, "time") as clock:
            cmd._gap(2.5)
            self.assertFalse(clock.sleep.called)   # nothing to be polite about yet
            cmd._gap(2.5)
            cmd._gap(2.5)
        self.assertEqual([c.args for c in clock.sleep.call_args_list], [(2.5,), (2.5,)])


class BudgetEscalationTests(TestCase):
    """A first attempt that runs out of room gets one retry at a bigger budget.

    The 2026-07-31 production run lost 3 of 84 cases to `error_max_turns` on long
    multi-defendant judgments. Escalation buys those back -- but ONLY on failure,
    because a case answered at the escalated budget was never the case the
    --eval run scored.
    """

    def _cmd(self):
        cmd = Command()
        cmd._downloads = 0
        return cmd

    def test_a_clean_first_attempt_never_escalates(self):
        """The common path must be untouched: one call, at the scored budget."""
        cmd = self._cmd()
        seen = []
        with mock.patch.object(Command, "_extract_one", autospec=True) as one:
            one.side_effect = lambda _s, _c, tier, max_tokens: (
                seen.append(max_tokens) or ("EX", "u", "m", 10)
            )
            result = cmd._extract(mock.Mock(), tier="premium")
        self.assertEqual(result, ("EX", "u", "m", 10, False))
        self.assertEqual(seen, [extract_verdicts.MAX_TOKENS])

    def test_exhaustion_retries_once_at_the_escalated_budget(self):
        cmd = self._cmd()
        seen = []

        def fake(_self, _case, *, tier, max_tokens):
            seen.append(max_tokens)
            if len(seen) == 1:
                raise RuntimeError(
                    "claude_cli error: Reached maximum number of turns (1)"
                )
            return ("EX", "u", "m", 99)

        with mock.patch.object(Command, "_extract_one", autospec=True, side_effect=fake):
            result = cmd._extract(mock.Mock(), tier="premium")
        self.assertEqual(result, ("EX", "u", "m", 99, True))
        self.assertEqual(
            seen, [extract_verdicts.MAX_TOKENS, extract_verdicts.ESCALATED_MAX_TOKENS]
        )

    def test_a_non_budget_failure_is_not_retried(self):
        """Retrying a dead document or a 403 at 4x the budget just costs 4x.

        This is the guard that keeps escalation from becoming a blanket retry.
        """
        cmd = self._cmd()
        calls = []

        def fake(_self, _case, *, tier, max_tokens):
            calls.append(max_tokens)
            raise RuntimeError("no order document yielded text")

        with mock.patch.object(Command, "_extract_one", autospec=True, side_effect=fake):
            with self.assertRaises(RuntimeError):
                cmd._extract(mock.Mock(), tier="premium")
        self.assertEqual(calls, [extract_verdicts.MAX_TOKENS])

    def test_escalation_can_still_fail_and_is_not_swallowed(self):
        """A case too long even for the bigger budget stays a loud failure.

        The whole design prefers a gap to a guess; escalation must not turn a
        second exhaustion into a silent success.
        """
        cmd = self._cmd()
        with mock.patch.object(
            Command, "_extract_one", autospec=True,
            side_effect=RuntimeError("error_max_turns"),
        ) as one:
            with self.assertRaises(RuntimeError):
                cmd._extract(mock.Mock(), tier="premium")
        self.assertEqual(one.call_count, 2)

    def test_the_exhaustion_matcher_reads_the_real_production_message(self):
        """Guard the guard: the matcher is a substring list, so pin it to the
        exact string the failing production run emitted."""
        self.assertTrue(
            extract_verdicts._is_exhaustion(
                RuntimeError(
                    "claude_cli error: Reached maximum number of turns (1)\n"
                    'Payload: {"subtype":"error_max_turns","is_error":true}'
                )
            )
        )
        self.assertFalse(
            extract_verdicts._is_exhaustion(RuntimeError("no order url on case"))
        )
        self.assertFalse(
            extract_verdicts._is_exhaustion(RuntimeError("403 organization disabled"))
        )

    def test_max_tokens_actually_reaches_the_model_call(self):
        """Guard the plumbing: _extract_one must PASS its budget, not ignore it.

        Before this change the invoke used the module constant directly, so a
        per-call budget would have been accepted and silently dropped -- the
        escalation would look like it worked while changing nothing.
        """
        cmd = self._cmd()
        case = mock.Mock(case_number="070-CR-0001")
        with mock.patch.object(extract_verdicts, "order_urls", return_value=["http://x/a.pdf"]), \
             mock.patch("review.converter.convert_all",
                        return_value=[{"conversion_status": "ok", "markdown": "फैसला"}]), \
             mock.patch("llm.routing.provider_for_tier"), \
             mock.patch("llm.invoke.invoke_text", return_value=_resp()) as invoke_text:
            cmd._extract_one(case, tier="premium", max_tokens=4242)
        self.assertEqual(invoke_text.call_args.args[2], 4242)
