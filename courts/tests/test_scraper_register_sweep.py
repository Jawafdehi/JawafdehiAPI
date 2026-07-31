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
                {"hearing_date": "2076-10-03", "case_status": "पेशी", "decision_type": ""},
                {"hearing_date": "2080-01-15", "case_status": "फैसला", "decision_type": "ठहर"},
            ]
        },
        entities=[{"side": "defendant", "name": "क", "address": None}],
    )
    base.update(over)
    return ParsedEnrichment(**base)


class TestUpsertFromDetail(_NgmTestCase):
    def test_creates_a_case_the_causelist_never_saw(self):
        assert upsert_from_detail("special", "076-CR-0294", _enrichment()) is True
        case = CourtCase.objects.using("ngm").get(court_id="special", case_number="076-CR-0294")
        assert case.registration_date_bs == "2076-11-13"
        assert case.case_type == "नक्कली प्रमाण पत्र"
        assert case.extra_data.get("source") == "register_sweep"

    def test_sets_listing_fields_apply_enrichment_would_not(self):
        # registration_date_* / case_type are outside _ENRICH_COLUMNS, so if the
        # create path didn't set them a swept case would have no dates at all.
        upsert_from_detail("special", "076-CR-0294", _enrichment())
        case = CourtCase.objects.using("ngm").get(court_id="special", case_number="076-CR-0294")
        assert case.registration_date_ad == date(2020, 2, 25)

    def test_still_applies_enrichment_columns(self):
        upsert_from_detail("special", "076-CR-0294", _enrichment())
        case = CourtCase.objects.using("ngm").get(court_id="special", case_number="076-CR-0294")
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
        assert not CourtCase.objects.using("ngm").filter(case_number="076-CR-0999").exists()

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
            court_id="special", case_number="076-CR-0294",
            side="defendant", name="क", nes_id=NES_IRI,
        )

        assert upsert_from_detail("special", "076-CR-0294", _enrichment()) is False

        case = CourtCase.objects.using("ngm").get(court_id="special", case_number="076-CR-0294")
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
        case = CourtCase.objects.using("ngm").get(court_id="special", case_number="076-CR-0294")
        assert case.is_deleted is True


class TestMaterialiseDetailHearings(_NgmTestCase):
    def test_creates_relational_rows_so_the_case_is_not_hearing_invisible(self):
        upsert_from_detail("special", "076-CR-0294", _enrichment())
        hearings = CourtCaseHearing.objects.using("ngm").filter(case_number="076-CR-0294")
        assert hearings.count() == 2
        assert {h.hearing_date_bs for h in hearings} == {"2076-10-03", "2080-01-15"}
        assert all(h.extra_data.get("source") == "register_sweep" for h in hearings)

    def test_leaves_unknown_fields_null_rather_than_inventing_them(self):
        upsert_from_detail("special", "076-CR-0294", _enrichment())
        h = CourtCaseHearing.objects.using("ngm").get(
            case_number="076-CR-0294", hearing_date_bs="2076-10-03"
        )
        # A detail page publishes no serial/bench/judges. Fabricating an ordinal
        # would put invented court data in a real column.
        assert h.serial_no is None
        assert h.judge_names is None
        assert h.bench is None

    def test_skips_unconvertible_dates_instead_of_sentinelling(self):
        # hearing_date_ad is NOT NULL and the cause-list path falls back to
        # 1900-01-01; seeding that here would pollute a clean column.
        e = _enrichment(extra_data={"enrichment_hearings": [
            {"hearing_date": "not-a-date"}, {"hearing_date": ""}, {},
        ]})
        upsert_from_detail("special", "076-CR-0295", e)
        assert not CourtCaseHearing.objects.using("ngm").filter(
            case_number="076-CR-0295"
        ).exists()
        assert not CourtCaseHearing.objects.using("ngm").filter(
            hearing_date_ad=date(1900, 1, 1)
        ).exists()

    def test_same_date_hearings_collapse_to_one_row(self):
        # Documented limitation: with no serial_no to separate them, two hearings
        # on one date dedupe. The full list survives in extra_data JSON.
        e = _enrichment(extra_data={"enrichment_hearings": [
            {"hearing_date": "2080-01-15", "case_status": "क"},
            {"hearing_date": "2080-01-15", "case_status": "ख"},
        ]})
        upsert_from_detail("special", "076-CR-0296", e)
        assert CourtCaseHearing.objects.using("ngm").filter(
            case_number="076-CR-0296"
        ).count() == 1
        case = CourtCase.objects.using("ngm").get(case_number="076-CR-0296")
        assert len(case.extra_data["enrichment_hearings"]) == 2

    def test_is_idempotent(self):
        upsert_from_detail("special", "076-CR-0294", _enrichment())
        again = materialise_detail_hearings("special", "076-CR-0294", _enrichment())
        assert again == 0
        assert CourtCaseHearing.objects.using("ngm").filter(
            case_number="076-CR-0294"
        ).count() == 2
