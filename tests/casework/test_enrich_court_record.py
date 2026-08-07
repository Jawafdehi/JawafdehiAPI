"""The court-record binder: dates, defendant resolution, and the patch it plans.

Coverage measured 2026-08-07 across the 307-case FY078/079 census: every court
case carries a registration date, 306 of 307 carry an end date (277 stated by
BOTH a deciding hearing and the case_status string, agreeing 277/277), and all
307 name at least one defendant.
"""

from casework.enrich_court_record import deciding_hearing, end_date, start_date


def _record(reg=None, hearings=(), status=None, parties=()):
    return {"court": "special", "number": "079-cr-0151",
            "detail": {"registration_date_ad": reg, "case_status": status},
            "hearings": list(hearings), "parties": list(parties)}


DECIDED = {"case_status": "फैसला", "decision_type": "सफाई",
           "hearing_date_ad": "2024-06-04", "hearing_date_bs": "2081-02-22"}
ADJOURNED = {"case_status": "स्थगित", "decision_type": "पक्षबाट",
             "hearing_date_ad": "2024-05-27", "hearing_date_bs": "2081-02-14"}


def test_start_date_is_the_court_registration_date():
    assert start_date([_record(reg="2023-06-22")]) == "2023-06-22"


def test_start_date_takes_the_earliest_across_references():
    records = [_record(reg="2024-01-01"), _record(reg="2023-06-22")]
    assert start_date(records) == "2023-06-22"


def test_start_date_is_empty_when_no_reference_carries_one():
    assert start_date([_record(reg=None)]) == ""


def test_deciding_hearing_is_picked_by_date_not_list_position():
    # Real ordering from special/079-CR-0151: the verdict sorts BEFORE an
    # earlier order in the API response.
    later = {**DECIDED, "hearing_date_ad": "2024-06-04"}
    earlier = {"case_status": "आदेश", "hearing_date_ad": "2024-06-03"}
    assert deciding_hearing([later, earlier]) == later


def test_deciding_hearing_takes_the_latest_of_several_verdict_rows():
    # Both rows pass the फैसला filter, so this can only pass by comparing
    # dates -- neither decided[0] nor decided[-1] would satisfy both asserts.
    first = {**DECIDED, "hearing_date_ad": "2024-01-01"}
    last = {**DECIDED, "hearing_date_ad": "2024-06-04"}
    assert deciding_hearing([first, last]) == last
    assert deciding_hearing([last, first]) == last


def test_deciding_hearing_ignores_non_deciding_rows():
    assert deciding_hearing([ADJOURNED]) is None


def test_end_date_comes_from_the_deciding_hearing():
    value, reason = end_date([_record(reg="2023-06-22", hearings=[ADJOURNED, DECIDED])])
    assert value == "2024-06-04"
    assert reason == ""


def test_end_date_falls_back_to_the_case_status_string():
    value, reason = end_date([_record(status="फैसला (मिती: २०८१/०२/२२)")])
    assert value == "2024-06-04"
    assert reason == ""


def test_an_open_case_gets_no_end_date():
    value, reason = end_date([_record(status="विचाराधीन", hearings=[ADJOURNED])])
    assert value == ""
    assert "no decision" in reason


def test_a_half_decided_case_gets_no_end_date():
    # Two references, only one decided. Writing an end date here would flip the
    # public status chip to "concluded" on a case still being heard.
    records = [_record(hearings=[DECIDED]), _record(status="विचाराधीन")]
    value, reason = end_date(records)
    assert value == ""
    assert "not every court reference" in reason


def test_end_date_takes_the_latest_when_every_reference_decided():
    records = [
        _record(hearings=[DECIDED]),
        _record(hearings=[{**DECIDED, "hearing_date_ad": "2025-01-15"}]),
    ]
    value, _ = end_date(records)
    assert value == "2025-01-15"


import urllib.error  # noqa: E402

from casework.common.api import EntityAlreadyExists  # noqa: E402
from casework.entity_identity import entity_slug  # noqa: E402
from casework.enrich_court_record import (  # noqa: E402
    PERSON_PREFIX,
    _is_person,
    exact_person_match,
    resolve_defendant,
)
from jawafdehi_shared.entities.ids import build_entity_iri  # noqa: E402

YADAV = "https://jawafdehi.org/entity/person/krishna-prasad-yadav"
ORG = "https://jawafdehi.org/entity/organization/krishna-prasad-yadav"


class _Results(list):
    """A plain list plus `.complete`, standing in for `CandidateList`."""
    complete = False


class _SearchApi:
    def __init__(self, results=(), created=None, complete=False):
        self.results, self.created, self.posted = list(results), created, []
        # Cautious by default, matching `CandidateList`'s own default: a test
        # that wants a bind on a single hit must say `complete=True` itself
        # rather than get it for free from an unmarked plain list.
        self.complete = complete

    def search_entities(self, query, **kwargs):
        results = _Results(self.results)
        results.complete = self.complete
        return results

    def create_entity(self, payload, timeout=60):
        self.posted.append(payload)
        if isinstance(self.created, Exception):
            raise self.created
        return self.created or {"@id": YADAV}


def _hit(nes_id, ne):
    return {"id": nes_id, "title": {"ne": ne}}


def test_a_row_carrying_an_nes_id_is_a_pure_copy():
    api = _SearchApi()
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", YADAV, citation="",
                            live_prefixes=["person"], run_entities={}, dry_run=True)
    assert (got.nes_id, got.how) == (YADAV, "nes_id")
    assert api.posted == []


def test_one_exact_person_match_binds():
    # A COMPLETE window with one hit is the clean case: nothing else can be
    # hiding, so the match is safe to bind.
    api = _SearchApi([_hit(YADAV, "कृष्ण प्रसाद यादव")], complete=True)
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=["person"], run_entities={}, dry_run=True)
    assert (got.nes_id, got.how) == (YADAV, "exact")


def test_a_single_hit_from_an_incomplete_window_does_not_bind():
    # Same premise as the two-namesake test below, caught one page earlier.
    # `संजय प्रसाद यादव` fills a full 50-row page and stops on relevance, so a
    # lone hit inside an INCOMPLETE window can have a dozen unseen twins just
    # past the edge -- exactly the failure this ladder exists to prevent.
    # `_SearchApi` defaults to `complete=False`, so this is the plain case.
    api = _SearchApi([_hit(YADAV, "कृष्ण प्रसाद यादव")])
    nes_id, reason = exact_person_match(api, "कृष्ण प्रसाद यादव")
    assert nes_id == ""
    assert "incomplete" in reason


def test_two_entities_with_the_same_exact_name_do_not_bind():
    # The namesake case. NES holds 13 rows for "संजय प्रसाद यादव". Two CONFIRMED
    # hits are ambiguous regardless of window completeness, so this is written
    # against the (default) incomplete window on purpose.
    twin = "https://jawafdehi.org/entity/person/krishna-prasad-yadav-2"
    api = _SearchApi([_hit(YADAV, "कृष्ण प्रसाद यादव"), _hit(twin, "कृष्ण प्रसाद यादव")])
    nes_id, reason = exact_person_match(api, "कृष्ण प्रसाद यादव")
    assert nes_id == ""
    assert "2 person entities" in reason


def test_a_non_person_entity_is_never_an_exact_match():
    api = _SearchApi([_hit(ORG, "कृष्ण प्रसाद यादव")])
    nes_id, reason = exact_person_match(api, "कृष्ण प्रसाद यादव")
    assert nes_id == ""
    assert "no person entity" in reason


def test_a_near_match_is_not_a_match():
    # कमला (feminine) must never satisfy कमल (masculine). The scored resolver
    # gives this 0.96 through the English title; equality gives it nothing.
    api = _SearchApi([_hit("https://jawafdehi.org/entity/person/kamala-thapa",
                           "कमला थापा")])
    nes_id, _ = exact_person_match(api, "कमल थापा")
    assert nes_id == ""


def test_no_match_creates_the_entity_and_binds_it():
    api = _SearchApi(results=[], created={"@id": YADAV})
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None,
                            citation="https://jawafdehi.org/material/court/special.079-cr-0151",
                            live_prefixes=["person"], run_entities={}, dry_run=False)
    assert (got.nes_id, got.how) == (YADAV, "created")
    assert api.posted[0]["prefix"] == "person"
    assert api.posted[0]["type"] == "Person"
    assert api.posted[0]["name"] == "कृष्ण प्रसाद यादव"


def test_a_dry_run_posts_nothing_but_reports_the_iri_it_would_use():
    api = _SearchApi(results=[])
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=["person"], run_entities={}, dry_run=True)
    assert got.how == "created"
    assert got.nes_id.startswith("https://jawafdehi.org/entity/person/")
    assert api.posted == []


def test_the_same_person_across_two_cases_creates_one_entity():
    api = _SearchApi(results=[], created={"@id": YADAV})
    run_entities = {}
    for _ in range(2):
        resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                          live_prefixes=["person"], run_entities=run_entities,
                          dry_run=False)
    assert len(api.posted) == 1


def test_an_existing_iri_collision_binds_the_existing_entity():
    # The stub raises before returning anything, so `resolve_defendant` never
    # reads the exception's payload -- a real 409 body is an opaque error blob,
    # not a clean IRI. What it keeps is the IRI it computed BEFORE the POST,
    # which by construction of a same-slug collision IS the entity that was
    # already there. `EntityAlreadyExists(YADAV)`'s argument is therefore
    # unread by design; expressed here as the actual `entity_slug` output
    # rather than `YADAV` itself, whose hand-picked spelling drops the schwas
    # `entity_slug` keeps (`कृष्ण प्रसाद यादव` -> `krishna-prasada-yadava`, not
    # `krishna-prasad-yadav`).
    api = _SearchApi(results=[], created=EntityAlreadyExists(YADAV))
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=["person"], run_entities={}, dry_run=False)
    assert got.how == "created"
    assert got.nes_id == build_entity_iri(PERSON_PREFIX,
                                          entity_slug("कृष्ण प्रसाद यादव"))


def test_a_name_that_cannot_be_slugged_fails_without_raising():
    api = _SearchApi(results=[])
    got = resolve_defendant(api, "   ", None, citation="",
                            live_prefixes=["person"], run_entities={}, dry_run=True)
    assert (got.nes_id, got.how) == ("", "failed")
    assert got.reason


def test_a_search_failure_fails_only_this_name():
    # `search_entities` -> `CaseworkApi.get` -> `_request` can raise
    # `urllib.error.HTTPError` on a transient 502; one bad row out of a case's
    # several defendants must not kill the run that is processing the rest.
    class _FlakyApi(_SearchApi):
        def search_entities(self, query, **kwargs):
            raise urllib.error.HTTPError("https://jawafdehi.org", 502,
                                         "Bad Gateway", {}, None)

    got = resolve_defendant(_FlakyApi(), "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=["person"], run_entities={}, dry_run=True)
    assert got.how == "failed"
    assert got.reason


def test_is_person_recognises_a_nested_person_category():
    # `person/politician` is a category NES nests under `person`, and it must
    # still count as a person -- the whole reason `_is_person` compares only
    # the first slash-segment rather than the whole prefix.
    assert _is_person(YADAV) is True
    assert _is_person(build_entity_iri("person/politician", "some-slug")) is True


def test_is_person_refuses_a_lookalike_prefix_and_other_types():
    # `personnel` shares a spelling prefix with `person` but is not one -- the
    # case a literal `startswith` would get wrong. A nested non-person prefix
    # (`organization/government`) must be refused too.
    assert _is_person(build_entity_iri("personnel", "someone")) is False
    assert _is_person(
        build_entity_iri("organization/government", "ministry-of-example")
    ) is False


def test_is_person_never_raises_on_a_malformed_iri():
    assert _is_person("not-a-valid-iri") is False
    assert _is_person("") is False
    assert _is_person(None) is False
