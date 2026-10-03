"""DB tests for the register-sweep write path (``upsert_from_detail``).

The sweep creates cases the cause-list crawler structurally cannot see. It writes
into a live corpus, so the tests here are mostly about what it must NOT do:
never touch an existing case (that would drop resolved ``nes_id`` links), never
act on a not-found parse, never seed sentinel dates.
"""

from datetime import date

from django.test import TestCase

from courts.models import CaseEntity, Court, CourtCase, CourtCaseHearing
from courts.scraper.base import materialise_detail_hearings, upsert_from_detail
from courts.scraper.rows import ParsedEnrichment

NES_IRI = "https://jawafdehi.org/entity/person/ram-bahadur"


class _NgmTestCase(TestCase):
    databases = "__all__"

    def setUp(self):
        Court.objects.using("ngm").get_or_create(
            identifier="special", defaults={"court_type": "", "full_name_nepali": ""}
        )


def _enrichment(**over):
    base = dict(
        core_fields={
            "registration_date_bs": "2076-11-13",
            "registration_date_ad": date(2020, 2, 25),
            "case_type": "नक्कली प्रमाण पत्र",
            "case_status": "फैसला (मिती: २०८०/०१/१५)",
        },
        extra_data={
            "enrichment_hearings": [
                {
                    "hearing_date": "2076-10-03",
                    "case_status": "पेशी",
                    "decision_type": "",
                },
                {
                    "hearing_date": "2080-01-15",
                    "case_status": "फैसला",
                    "decision_type": "ठहर",
                },
            ]
        },
        entities=[{"side": "defendant", "name": "क", "address": None}],
    )
    base.update(over)
    return ParsedEnrichment(**base)


class TestUpsertFromDetail(_NgmTestCase):
    def test_creates_a_case_the_causelist_never_saw(self):
        assert upsert_from_detail("special", "076-CR-0294", _enrichment()) is True
        case = CourtCase.objects.using("ngm").get(
            court_id="special", case_number="076-CR-0294"
        )
        assert case.registration_date_bs == "2076-11-13"
        assert case.case_type == "नक्कली प्रमाण पत्र"
        assert case.extra_data.get("source") == "register_sweep"

    def test_sets_listing_fields_apply_enrichment_would_not(self):
        # registration_date_* / case_type are outside _ENRICH_COLUMNS, so if the
        # create path didn't set them a swept case would have no dates at all.
        upsert_from_detail("special", "076-CR-0294", _enrichment())
        case = CourtCase.objects.using("ngm").get(
            court_id="special", case_number="076-CR-0294"
        )
        assert case.registration_date_ad == date(2020, 2, 25)

    def test_still_applies_enrichment_columns(self):
        upsert_from_detail("special", "076-CR-0294", _enrichment())
        case = CourtCase.objects.using("ngm").get(
            court_id="special", case_number="076-CR-0294"
        )
        assert case.status == "enriched"
        assert case.verdict_type  # derived from the decisive hearing / status

    def test_writes_the_parties(self):
        upsert_from_detail("special", "076-CR-0294", _enrichment())
        names = list(
            CaseEntity.objects.using("ngm")
            .filter(court_id="special", case_number="076-CR-0294")
            .values_list("name", flat=True)
        )
        assert names == ["क"]

    def test_refuses_a_not_found_parse(self):
        empty = ParsedEnrichment(
            core_fields={},
            # A not-found page on supreme/district/high still yields these keys.
            extra_data={"enrichment_hearings": [], "enrichment_timeline": []},
            entities=[],
        )
        assert upsert_from_detail("special", "076-CR-0999", empty) is False
        assert (
            not CourtCase.objects.using("ngm")
            .filter(case_number="076-CR-0999")
            .exists()
        )

    def test_never_touches_an_existing_case(self):
        """The nes_id guard — the whole reason the sweep is add-only.

        Re-enriching a known case calls _replace_entities, which deletes every
        party row and recreates it without nes_id. Only 607 such links exist in
        the entire 4.6M-row corpus, and they are all special-court defendants.
        """
        CourtCase.objects.using("ngm").create(
            court_id="special", case_number="076-CR-0294", case_type="पुरानो"
        )
        CaseEntity.objects.using("ngm").create(
            court_id="special",
            case_number="076-CR-0294",
            side="defendant",
            name="क",
            nes_id=NES_IRI,
        )

        assert upsert_from_detail("special", "076-CR-0294", _enrichment()) is False

        case = CourtCase.objects.using("ngm").get(
            court_id="special", case_number="076-CR-0294"
        )
        assert case.case_type == "पुरानो", "an existing case must not be rewritten"
        entity = CaseEntity.objects.using("ngm").get(case_number="076-CR-0294")
        assert entity.nes_id == NES_IRI, "resolved entity link was destroyed"

    def test_soft_deleted_case_is_not_resurrected(self):
        # A soft-deleted row still occupies its register slot. Re-creating it
        # would undo a deliberate deletion.
        CourtCase.objects.using("ngm").create(
            court_id="special", case_number="076-CR-0294", is_deleted=True
        )
        assert upsert_from_detail("special", "076-CR-0294", _enrichment()) is False
        case = CourtCase.objects.using("ngm").get(
            court_id="special", case_number="076-CR-0294"
        )
        assert case.is_deleted is True


class TestMaterialiseDetailHearings(_NgmTestCase):
    def test_creates_relational_rows_so_the_case_is_not_hearing_invisible(self):
        upsert_from_detail("special", "076-CR-0294", _enrichment())
        hearings = CourtCaseHearing.objects.using("ngm").filter(
            case_number="076-CR-0294"
        )
        assert hearings.count() == 2
        assert {h.hearing_date_bs for h in hearings} == {"2076-10-03", "2080-01-15"}
        assert all(h.extra_data.get("source") == "register_sweep" for h in hearings)

    def test_leaves_unknown_fields_null_rather_than_inventing_them(self):
        upsert_from_detail("special", "076-CR-0294", _enrichment())
        h = CourtCaseHearing.objects.using("ngm").get(
            case_number="076-CR-0294", hearing_date_bs="2076-10-03"
        )
        # No detail page publishes a serial or a bench. Fabricating an ordinal
        # would put invented court data in a real column. Judges are a different
        # case — the special court's detail page carries none, but supreme's and
        # district's do, so judge_names is null here and populated there.
        assert h.serial_no is None
        assert h.judge_names is None
        assert h.bench is None

    def test_skips_unconvertible_dates_instead_of_sentinelling(self):
        # hearing_date_ad is NOT NULL and the cause-list path falls back to
        # 1900-01-01; seeding that here would pollute a clean column.
        e = _enrichment(
            extra_data={
                "enrichment_hearings": [
                    {"hearing_date": "not-a-date"},
                    {"hearing_date": ""},
                    {},
                ]
            }
        )
        upsert_from_detail("special", "076-CR-0295", e)
        assert (
            not CourtCaseHearing.objects.using("ngm")
            .filter(case_number="076-CR-0295")
            .exists()
        )
        assert (
            not CourtCaseHearing.objects.using("ngm")
            .filter(hearing_date_ad=date(1900, 1, 1))
            .exists()
        )

    def test_same_date_hearings_collapse_to_one_row(self):
        # Documented limitation: with no serial_no to separate them, two hearings
        # on one date dedupe. The full list survives in extra_data JSON.
        e = _enrichment(
            extra_data={
                "enrichment_hearings": [
                    {"hearing_date": "2080-01-15", "case_status": "क"},
                    {"hearing_date": "2080-01-15", "case_status": "ख"},
                ]
            }
        )
        upsert_from_detail("special", "076-CR-0296", e)
        assert (
            CourtCaseHearing.objects.using("ngm")
            .filter(case_number="076-CR-0296")
            .count()
            == 1
        )
        case = CourtCase.objects.using("ngm").get(case_number="076-CR-0296")
        assert len(case.extra_data["enrichment_hearings"]) == 2

    def test_is_idempotent(self):
        upsert_from_detail("special", "076-CR-0294", _enrichment())
        again = materialise_detail_hearings("special", "076-CR-0294", _enrichment())
        assert again == 0
        assert (
            CourtCaseHearing.objects.using("ngm")
            .filter(case_number="076-CR-0294")
            .count()
            == 2
        )


class TestMaterialiseEveryParserShape(_NgmTestCase):
    """Every parser's ``enrichment_hearings`` shape, not just the special court's.

    The four parsers disagree on the key names inside that list, and for a long
    time the materialiser read only the ``special``/``high`` names. Against a
    ``supreme`` or ``district`` payload it therefore skipped every hearing and
    returned 0 — writing nothing, raising nothing, logging nothing. Roughly 180k
    swept cases ended up holding a full timeline in JSON and no relational rows,
    which every hearing-level query reads as "this case was never listed".

    A single-court fixture is what let that through, so each shape is asserted
    here against a payload copied from a real stored row.
    """

    def setUp(self):
        super().setUp()
        for identifier in ("supreme", "kathmandudc", "patanhc"):
            Court.objects.using("ngm").get_or_create(
                identifier=identifier,
                defaults={"court_type": "", "full_name_nepali": ""},
            )

    def test_supreme_shape_writes_rows_with_status_order_and_judges(self):
        # Copied from supreme 078-CR-0041: date/status/order_type/judges.
        e = _enrichment(
            extra_data={
                "enrichment_hearings": [
                    {
                        "date": "2078-11-03",
                        "type": "hearing",
                        "judges": "मा.न्या. श्री दीपककुमार कार्की, मा.न्या. श्री सपना प्रधान मल्ल,",
                        "status": "हेर्न नभ्याइने",
                    },
                    {
                        "date": "2082-03-20",
                        "type": "hearing",
                        "judges": "मा.न्या. श्री सपना प्रधान मल्ल, मा.न्या. श्री नृपध्वज निरौला,",
                        "status": "फैसला",
                        "order_type": "सदर",
                    },
                ]
            }
        )
        assert materialise_detail_hearings("supreme", "078-CR-0041", e) == 2
        last = CourtCaseHearing.objects.using("ngm").get(
            court_id="supreme", case_number="078-CR-0041", hearing_date_bs="2082-03-20"
        )
        assert last.case_status == "फैसला"
        assert last.decision_type == "सदर"
        assert last.judge_names == (
            "मा.न्या. श्री सपना प्रधान मल्ल, मा.न्या. श्री नृपध्वज निरौला,"
        )

    def test_district_shape_writes_rows_with_order_and_judge(self):
        # District emits judge/order singular, and no status column at all.
        e = _enrichment(
            extra_data={
                "enrichment_hearings": [
                    {
                        "date": "2080-05-12",
                        "type": "पेशी",
                        "division": "इजलास १",
                        "judge": "मा.न्या. श्री राम बहादुर",
                        "order": "तारेख तोकिएको",
                    }
                ]
            }
        )
        assert materialise_detail_hearings("kathmandudc", "080-CR-0001", e) == 1
        h = CourtCaseHearing.objects.using("ngm").get(
            court_id="kathmandudc", case_number="080-CR-0001"
        )
        assert h.decision_type == "तारेख तोकिएको"
        assert h.judge_names == "मा.न्या. श्री राम बहादुर"
        # ``type`` is the sitting's kind, not its outcome — it must not be read
        # as a status.
        assert h.case_status is None

    def test_high_shape_still_uses_the_original_key_names(self):
        # The shape that always worked. Guards against an alias change that fixes
        # supreme by breaking the 236k rows already written for the high courts.
        e = _enrichment(
            extra_data={
                "enrichment_hearings": [
                    {
                        "hearing_date": "2079-06-10",
                        "case_status": "पेशी",
                        "decision_type": "स्थगित",
                    }
                ]
            }
        )
        assert materialise_detail_hearings("patanhc", "079-CR-0002", e) == 1
        h = CourtCaseHearing.objects.using("ngm").get(
            court_id="patanhc", case_number="079-CR-0002"
        )
        assert h.case_status == "पेशी"
        assert h.decision_type == "स्थगित"

    def test_a_shape_with_no_recognised_date_key_still_writes_nothing(self):
        # The original failure mode, kept as a test: an unrecognised payload must
        # skip quietly rather than invent a date.
        e = _enrichment(
            extra_data={"enrichment_hearings": [{"मिती": "2079-06-10"}, None]}
        )
        assert materialise_detail_hearings("supreme", "079-CR-0003", e) == 0
