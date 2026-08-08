"""The court-record binder: dates, defendant resolution, and the patch it plans.

Coverage measured 2026-08-07 across the 307-case FY078/079 census: every court
case carries a registration date, 306 of 307 carry an end date (277 stated by
BOTH a deciding hearing and the case_status string, agreeing 277/277), and all
307 name at least one defendant.
"""

from casework.enrich_court_record import deciding_hearing, end_date, start_date


def _record(reg=None, hearings=(), status=None, parties=(), number="079-cr-0151"):
    return {"court": "special", "number": number,
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
from casework.entity_resolver import normalise_name  # noqa: E402
from casework.enrich_court_record import (  # noqa: E402
    PERSON_PREFIX,
    _accused_binds,
    _is_person,
    defendant_name_index,
    exact_person_match,
    held_names,
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


def test_an_existing_iri_collision_refuses_to_bind():
    # A 409 means the slug is TAKEN -- by an entity the ladder just declined to
    # identify, since a unique exact match would have bound at rung 2 and never
    # reached the POST. Keeping the pre-POST IRI and binding it (what this did
    # until 2026-08-07) hands the case to whoever already owns that slug: after
    # "13 person entities carry this exact name", or after the truncation veto
    # declined candidate X, the create collides with X and X gets bound anyway
    # with no ambiguity check. Nothing may be bound here.
    api = _SearchApi(results=[], created=EntityAlreadyExists(YADAV))
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=["person"], run_entities={}, dry_run=False)
    assert (got.nes_id, got.how) == ("", "failed")
    # The report must name the IRI that was taken, so a human can look at it.
    # Spelled through `entity_slug` rather than as `YADAV`, whose hand-picked
    # spelling drops the schwas `entity_slug` keeps (`कृष्ण प्रसाद यादव` ->
    # `krishna-prasada-yadava`, not `krishna-prasad-yadav`).
    taken = build_entity_iri(PERSON_PREFIX, entity_slug("कृष्ण प्रसाद यादव"))
    assert taken in got.reason
    assert "collided" in got.reason


def test_a_collision_is_not_remembered_for_the_rest_of_the_run():
    # The refusal must not poison `run_entities` either: caching the taken IRI
    # would make every LATER case naming this defendant bind it at the "reused
    # from this run" rung, turning one refused bind into a run-wide one.
    api = _SearchApi(results=[], created=EntityAlreadyExists(YADAV))
    run_entities = {}
    resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                      live_prefixes=["person"], run_entities=run_entities,
                      dry_run=False)
    assert run_entities == {}


def test_two_same_named_defendants_on_different_cases_do_not_collapse():
    # `run_entities` is shared across cases so ONE person named on two cases
    # becomes one entity. Keyed on the bare name that reuse is indiscriminate:
    # two DIFFERENT people who merely share a name -- a defendant on case A and
    # a defendant on case B -- collapse into a single entity, and case A's
    # person then carries case B's accusation. The court party row's `address`
    # is what the charge sheet uses to tell them apart, so it is part of the key.
    #
    # Written against a stub that behaves like the server (one slug, one
    # entity), because both halves of the fix have to hold for this to pass:
    # keying on the bare name reuses A's entity for B outright, and keeping the
    # old 409 handling binds A's entity to B after the create collides.
    class _SlugAwareApi(_SearchApi):
        def create_entity(self, payload, timeout=60):
            taken = {p["slug"] for p in self.posted}
            self.posted.append(payload)
            iri = build_entity_iri(PERSON_PREFIX, payload["slug"])
            if payload["slug"] in taken:
                raise EntityAlreadyExists(iri)
            return {"@id": iri}

    api = _SlugAwareApi(results=[])
    run_entities = {}
    common = {"citation": "", "live_prefixes": ["person"],
              "run_entities": run_entities, "dry_run": False}
    first = resolve_defendant(api, "कृष्ण प्रसाद यादव", None,
                              address="सर्लाही, हरिपुर-४", **common)
    second = resolve_defendant(api, "कृष्ण प्रसाद यादव", None,
                               address="मोरङ, विराटनगर-१२", **common)
    assert (first.how, bool(first.nes_id)) == ("created", True)
    # A different person must not inherit the first one's entity, by either
    # route -- not from the run cache, and not from the collision.
    assert second.nes_id != first.nes_id
    assert (second.nes_id, second.how) == ("", "failed")


def test_the_same_person_on_two_cases_still_creates_one_entity():
    # The other half of the same key: same name AND same address is one person,
    # and must still be created once no matter how many cases name them --
    # otherwise the address in the key would have cost the cross-case reuse
    # `run_entities` exists for.
    api = _SearchApi(results=[], created={"@id": YADAV})
    run_entities = {}
    for _ in range(2):
        resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                          live_prefixes=["person"], run_entities=run_entities,
                          dry_run=False, address="सर्लाही, हरिपुर-४")
    assert len(api.posted) == 1
    assert len(run_entities) == 1


def test_an_address_is_normalised_before_it_keys_the_run():
    # Spacing/punctuation drift in the portal's transcription of one address
    # must not split one person into two entities -- the same `normalise_name`
    # the name half of the key already goes through.
    api = _SearchApi(results=[], created={"@id": YADAV})
    run_entities = {}
    for address in ("सर्लाही, हरिपुर-४", " सर्लाही,  हरिपुर-४ "):
        resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                          live_prefixes=["person"], run_entities=run_entities,
                          dry_run=False, address=address)
    assert len(api.posted) == 1


def test_an_unreadable_prefix_list_is_not_a_verdict_on_the_prefix():
    # `read_live_prefixes` returns None on any error (a transient 502 at run
    # start), and `prefix_is_creatable` folds None to the empty set -- so
    # without a dedicated branch every defendant needing creation across all
    # 307 cases is reported "the person prefix is not creatable", a false
    # statement about a prefix as ordinary as `person`. The dates still PATCH,
    # so a re-run finds them populated and the missing binds look deliberate.
    api = _SearchApi(results=[])
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=None, run_entities={}, dry_run=True)
    assert (got.nes_id, got.how) == ("", "failed")
    assert "could not be read" in got.reason and "retry this case" in got.reason
    # The distinction is the whole point: this must NOT read as a judgement on
    # the prefix the way a genuinely refused prefix does.
    assert "not creatable" not in got.reason
    assert api.posted == []


def test_a_genuinely_unusable_prefix_still_says_so():
    # The companion: an EMPTY (successfully read) prefix list is a real verdict
    # -- `person` is in use nowhere -- and must keep saying "not creatable",
    # or the None branch above would have swallowed both cases into one reason.
    api = _SearchApi(results=[])
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=[], run_entities={}, dry_run=True)
    assert (got.nes_id, got.how) == ("", "failed")
    assert "not creatable" in got.reason


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


from casework.enrich_court_record import (  # noqa: E402
    ACQUITTED,
    CHARGED,
    CasePlan,
    bind_outcome,
    plan_case,
)

CASE_IRI = "https://jawafdehi.org/courtcase/special/079-cr-0151"


class _PlanApi(_SearchApi):
    def __init__(self, detail=None, hearings=(), parties=(), **kw):
        super().__init__(**kw)
        self._detail, self._hearings, self._parties = detail or {}, list(hearings), list(parties)

    def get_courtcase(self, court, number, timeout=60):
        return self._detail

    def list_hearings(self, court, number, timeout=60):
        return self._hearings

    def get_court_case_entities(self, court, number, timeout=60):
        return self._parties


def _case(**over):
    base = {"slug": "case-079-cr-0151", "state": "DRAFT", "court_cases": [CASE_IRI],
            "case_start_date": None, "case_end_date": None, "entities": []}
    base.update(over)
    return base


def _plan(api, case, **kw):
    kw.setdefault("live_prefixes", ["person"])
    kw.setdefault("run_entities", {})
    kw.setdefault("dry_run", True)
    kw.setdefault("held", {})
    return plan_case(api, case, 'W/"7"', **kw)


def test_a_whole_case_acquittal_labels_every_defendant_acquitted():
    assert bind_outcome([_record(hearings=[DECIDED])]) == ACQUITTED


def test_a_conviction_still_labels_defendants_charged():
    convicted = {**DECIDED, "decision_type": "ठहर"}
    assert bind_outcome([_record(hearings=[convicted])]) == CHARGED


def test_a_partial_conviction_labels_defendants_charged():
    partial = {**DECIDED, "decision_type": "आंशिक ठहर"}
    assert bind_outcome([_record(hearings=[partial])]) == CHARGED


def test_an_undecided_case_labels_defendants_charged():
    assert bind_outcome([_record(status="विचाराधीन")]) == CHARGED


def test_a_decided_reference_plus_an_undecided_one_is_charged():
    # One reference decided सफाई, the other still open. Half-decided is not
    # decided -- the same doctrine `end_date` already applies -- so this must
    # not acquit a case that is still being heard.
    records = [_record(hearings=[DECIDED]), _record(status="विचाराधीन")]
    assert bind_outcome(records) == CHARGED


def test_a_decided_acquittal_plus_a_conviction_is_charged():
    convicted = {**DECIDED, "decision_type": "ठहर"}
    records = [_record(hearings=[DECIDED]), _record(hearings=[convicted])]
    assert bind_outcome(records) == CHARGED


def test_a_reference_decided_only_via_case_status_cannot_acquit():
    # This reference decided (the paren-date form parses to a date), but
    # carries no hearing row and therefore no outcome text at all -- it can
    # never be confirmed a plain acquittal, so mixed with a सफाई hearing on
    # the other reference the case still reads CHARGED.
    records = [_record(hearings=[DECIDED]),
               _record(status="फैसला (मिती: २०८१/०२/२२)")]
    assert bind_outcome(records) == CHARGED


def test_a_qualified_acquittal_cell_is_not_a_plain_acquittal():
    # The corpus contains compounds that qualify सफाई rather than standing
    # alone. A bare substring test on सफाई would wrongly acquit here, the same
    # class of bug `courts.case_status` fixed for ठहर (593 court_cases once
    # recorded CONVICTED from a cell that actually said आंशिक ...ठहर).
    qualified = {**DECIDED, "decision_type": "आंशिक सफाई"}
    assert bind_outcome([_record(hearings=[qualified])]) == CHARGED


def test_a_misspelled_qualifier_still_blocks_the_acquittal():
    # `आंशीक` (दीर्घ ई) is a real portal misspelling of `आंशिक`, documented in
    # `courts.case_status._ORDER_SPELLING`. An exact-string qualifier check
    # would miss it and read this cell as a plain acquittal -- every defendant
    # on a partially-convicted case would then be labelled acquitted. Proves
    # the cell is normalised (via `_order_key`) before the qualifier test.
    misspelled = {**DECIDED, "decision_type": "आंशीक सफाई"}
    assert bind_outcome([_record(hearings=[misspelled])]) == CHARGED


def test_every_reference_deciding_a_plain_acquittal_is_acquitted():
    # Both references decided सफाई, but through the two DIFFERENT sources
    # `_reference_end` itself draws on. Reference 1's decided-ness (and its
    # outcome text) come straight off its own hearing row, which carries a
    # usable `hearing_date_ad`. Reference 2's hearing carries the outcome text
    # but NO usable `hearing_date_ad`, so its decided-ness falls through to
    # the `case_status` paren-date fallback -- the same two-source path
    # `_reference_end` uses for `end_date`, now exercised on the ACQUITTED
    # branch rather than only the CHARGED one. Nothing before this test
    # proved the positive path survives the `all(decided) and all(acquitted)`
    # rewrite -- every earlier multi-reference test asserted CHARGED.
    acquittal_no_hearing_date = {"case_status": "फैसला", "decision_type": "सफाई"}
    records = [
        _record(hearings=[DECIDED]),
        _record(status="फैसला (मिती: २०८१/०२/२२)", hearings=[acquittal_no_hearing_date]),
    ]
    assert bind_outcome(records) == ACQUITTED


def test_the_plan_carries_both_dates_and_the_accused_binds():
    api = _PlanApi(
        detail={"registration_date_ad": "2023-06-22"},
        hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV},
                 {"side": "plaintiff", "name": "नेपाल सरकार"}],
    )
    plan = _plan(api, _case())
    assert dict(plan.fields) == {"case_start_date": "2023-06-22",
                                 "case_end_date": "2024-06-04"}
    assert plan.entities == [{"nes_id": YADAV, "relationship_type": "accused",
                              "outcome": ACQUITTED,
                              "notes": "प्रतिवादी — विशेष अदालत मुद्दा 079-cr-0151"}]
    assert plan.status == "would-patch"


def test_a_plaintiff_is_never_bound():
    api = _PlanApi(detail={"registration_date_ad": "2023-06-22"},
                   parties=[{"side": "plaintiff", "name": "नेपाल सरकार"}])
    assert _plan(api, _case()).entities is None


def test_accused_binds_skips_a_non_prosecution_record_but_binds_a_prosecution_one():
    # The OA party is deliberately named something other than नेपाल सरकार (its
    # real value per the brief's probe): a wrong implementation that filters
    # on THAT literal name string would still pass a fixture using it, for the
    # wrong reason. Naming it "कुनै व्यक्ति" (an ordinary person's placeholder)
    # means only a code-based filter can make this record skip.
    cr_record = _record(number="079-cr-0151",
                        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव",
                                  "nes_id": YADAV}])
    oa_record = _record(number="079-oa-0014",
                        parties=[{"side": "defendant", "name": "कुनै व्यक्ति"}])
    items, rows, skips = _accused_binds(
        _SearchApi(), _case(), [cr_record, oa_record],
        live_prefixes=["person"], run_entities={}, dry_run=True, held={})
    assert [i["nes_id"] for i in items] == [YADAV]
    assert [r["name"] for r in rows] == ["कृष्ण प्रसाद यादव"]
    assert len(skips) == 1
    assert "079-oa-0014" in skips[0] and "OA" in skips[0]


def test_accused_binds_binds_the_pre_fy073_no_code_format():
    # `93-068-0194`-style numbers carry no `-<letters>-` segment at all -- 139
    # references in the corpus. A rule spelled "the number must contain
    # `-CR-`" would misclassify this as an unrecognised code and silently
    # drop these prosecutions.
    record = _record(number="93-068-0194",
                     parties=[{"side": "defendant", "name": "सिताराम यादव",
                               "nes_id": YADAV}])
    items, rows, skips = _accused_binds(
        _SearchApi(), _case(), [record],
        live_prefixes=["person"], run_entities={}, dry_run=True, held={})
    assert [i["nes_id"] for i in items] == [YADAV]
    assert skips == []


def test_accused_binds_skips_an_unrecognised_code_not_on_any_documented_skip_list():
    # A deny-list rewrite (skip only OA/RE/WC/WF/WH/WO) would still bind this:
    # "ZZ" is on neither list. It must skip anyway -- an unrecognised code
    # risks naming an office, and skipping one only costs a bind a later run
    # recovers, so the allow-list, not a deny-list, is what must gate this.
    record = _record(number="079-zz-0001",
                     parties=[{"side": "defendant", "name": "कुनै व्यक्ति", "nes_id": YADAV}])
    items, rows, skips = _accused_binds(
        _SearchApi(), _case(), [record],
        live_prefixes=["person"], run_entities={}, dry_run=True, held={})
    assert items == []
    assert len(skips) == 1 and "ZZ" in skips[0]


def test_accused_binds_binds_a_person_named_through_their_firm():
    # FJ's one reference in the corpus names a proprietor through their firm:
    # "अनिल गुप्ता एण्ड एशोसियटस का प्रोपराइटर अनिल कुमार गुप्ता". A keyword
    # filter on "एशोसियटस" or "कार्यालय" would drop this real defendant --
    # only the code, never the name text, may gate the bind.
    firm_name = "अनिल गुप्ता एण्ड एशोसियटस का प्रोपराइटर अनिल कुमार गुप्ता"
    record = _record(number="079-fj-0001",
                     parties=[{"side": "defendant", "name": firm_name, "nes_id": YADAV}])
    items, rows, skips = _accused_binds(
        _SearchApi(), _case(), [record],
        live_prefixes=["person"], run_entities={}, dry_run=True, held={})
    assert [i["nes_id"] for i in items] == [YADAV]
    assert skips == []


def test_defendant_name_index_groups_by_normalised_name_across_cases():
    # Extra spacing and a trailing danda on case-b's spelling: a wrong
    # implementation keyed on raw string equality would put these in two
    # separate buckets instead of one, and never hold either.
    records_by_slug = {
        "case-a": [_record(parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}])],
        "case-b": [_record(number="080-cr-0002",
                           parties=[{"side": "defendant", "name": "कृष्ण  प्रसाद यादव।"}])],
    }
    index = defendant_name_index(records_by_slug)
    assert index[normalise_name("कृष्ण प्रसाद यादव")] == frozenset({"case-a", "case-b"})


def test_defendant_name_index_excludes_non_prosecution_records():
    # A ministry named "defendant" on two OA references must not consume a
    # review slot: it was never a bind candidate, so it must never surface as
    # held either. A wrong implementation that indexes every party regardless
    # of case-type code would put this name in the index with two slugs, and
    # `held_names` would then flag it.
    records_by_slug = {
        "case-a": [_record(number="079-oa-0014",
                           parties=[{"side": "defendant", "name": "कुनै व्यक्ति"}])],
        "case-b": [_record(number="080-oa-0002",
                           parties=[{"side": "defendant", "name": "कुनै व्यक्ति"}])],
    }
    assert defendant_name_index(records_by_slug) == {}


def test_held_names_is_empty_when_every_name_is_on_one_case_only():
    records_by_slug = {
        "case-a": [_record(parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}])],
        "case-b": [_record(number="080-cr-0002",
                           parties=[{"side": "defendant", "name": "सिताराम यादव"}])],
    }
    assert held_names(defendant_name_index(records_by_slug)) == {}


def test_held_names_names_the_cases_a_shared_defendant_appears_on():
    records_by_slug = {
        "case-a": [_record(parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}])],
        "case-b": [_record(number="080-cr-0002",
                           parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}])],
    }
    held = held_names(defendant_name_index(records_by_slug))
    assert held == {normalise_name("कृष्ण प्रसाद यादव"): frozenset({"case-a", "case-b"})}


def test_a_shared_defendant_is_held_on_both_cases_naming_the_other():
    # Both cases must be held, and each one's reason must name the OTHER case,
    # not itself -- a wrong implementation that reports the full `held[key]`
    # set unfiltered would pass "both held" but also claim case-a appears on
    # case-a, which the second half of each assertion below catches.
    key = normalise_name("कृष्ण प्रसाद यादव")
    held = {key: frozenset({"case-a", "case-b"})}
    record = _record(parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}])
    items_a, rows_a, _ = _accused_binds(
        _SearchApi(), _case(slug="case-a"), [record],
        live_prefixes=["person"], run_entities={}, dry_run=True, held=held)
    items_b, rows_b, _ = _accused_binds(
        _SearchApi(), _case(slug="case-b"), [record],
        live_prefixes=["person"], run_entities={}, dry_run=True, held=held)
    assert items_a == [] and items_b == []
    assert rows_a[0]["how"] == "held" and rows_b[0]["how"] == "held"
    assert "case-b" in rows_a[0]["reason"] and "case-a" not in rows_a[0]["reason"]
    assert "case-a" in rows_b[0]["reason"] and "case-b" not in rows_b[0]["reason"]


def test_a_name_held_for_another_name_does_not_hold_this_one():
    # `held` is non-empty, but carries no entry for THIS defendant's name -- a
    # wrong implementation that treats "held is non-empty" as "hold everyone
    # on this case" would still fail this, since the bound defendant carries a
    # real `nes_id` and a bind item only appears when the ladder actually ran.
    held = {normalise_name("अर्को व्यक्ति"): frozenset({"case-x", "case-y"})}
    record = _record(parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव",
                               "nes_id": YADAV}])
    items, rows, _ = _accused_binds(
        _SearchApi(), _case(slug="case-a"), [record],
        live_prefixes=["person"], run_entities={}, dry_run=True, held=held)
    assert [i["nes_id"] for i in items] == [YADAV]
    assert rows[0]["how"] == "nes_id"


def test_a_name_spelled_with_different_punctuation_across_cases_still_holds():
    # End to end from raw records through `defendant_name_index`/`held_names`
    # into `_accused_binds`: keying on `normalise_name` (the same function
    # `exact_person_match` uses) means a spacing/punctuation variant cannot
    # slip past the held check the way raw string equality would.
    records_by_slug = {
        "case-a": [_record(parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}])],
        "case-b": [_record(number="080-cr-0002",
                           parties=[{"side": "defendant", "name": "कृष्ण  प्रसाद यादव।"}])],
    }
    held = held_names(defendant_name_index(records_by_slug))
    items, rows, _ = _accused_binds(
        _SearchApi(), _case(slug="case-a"), records_by_slug["case-a"],
        live_prefixes=["person"], run_entities={}, dry_run=True, held=held)
    assert items == []
    assert rows[0]["how"] == "held"


def test_two_punctuation_variants_of_one_name_on_the_same_case_collapse_to_one_row():
    # `seen` used to key on the raw name, so two spellings of the SAME
    # defendant on one case's parties produced two `defendant_resolve` rows
    # (and could double-bind the same person under two different IRIs) for
    # one person. `defendant_name_index` already collapses spelling variants
    # via `normalise_name`; the per-case dedup inside `_accused_binds` must
    # agree, or a case can hold one spelling while binding the other.
    record = _record(parties=[
        {"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV},
        {"side": "defendant", "name": "कृष्ण  प्रसाद यादव।"},
    ])
    items, rows, _ = _accused_binds(
        _SearchApi(), _case(), [record],
        live_prefixes=["person"], run_entities={}, dry_run=True, held={})
    assert len(rows) == 1
    assert [i["nes_id"] for i in items] == [YADAV]


def test_a_held_entry_mapping_to_an_empty_set_still_holds():
    # `held_names` only ever returns entries with 2+ slugs, so an empty set
    # should not occur in practice -- but the membership check must be
    # `is not None`, not truthiness. A truthy check on an empty frozenset
    # falls through and binds, which is the fail-OPEN direction on exactly
    # the defamation path this task exists to close.
    held = {normalise_name("कृष्ण प्रसाद यादव"): frozenset()}
    record = _record(parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव",
                               "nes_id": YADAV}])
    items, rows, _ = _accused_binds(
        _SearchApi(), _case(), [record],
        live_prefixes=["person"], run_entities={}, dry_run=True, held=held)
    assert items == []
    assert rows[0]["how"] == "held"


def test_a_case_with_only_a_non_prosecution_reference_still_gets_both_dates():
    # Guards "dates are not filtered": `plan_case` reads `start_date`/`end_date`
    # off `records` directly, so the accused-bind filter must live inside
    # `_accused_binds` and never upstream in `court_record_for_case` -- if it
    # did, this case's only reference would vanish from `records` entirely and
    # both date fields would stay empty rather than just the bind.
    api = _PlanApi(detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
                   parties=[{"side": "defendant", "name": "कुनै व्यक्ति"}])
    case = _case(court_cases=["https://jawafdehi.org/courtcase/special/079-oa-0014"])
    plan = _plan(api, case)
    assert dict(plan.fields) == {"case_start_date": "2023-06-22",
                                 "case_end_date": "2024-06-04"}
    assert plan.entities is None
    assert any("079-oa-0014" in s and "OA" in s for s in plan.skips)


def test_an_existing_bind_survives_untouched():
    # The REAL read shape: the relationship type comes back under `type`, and
    # `relationship_type` never appears on a read at all. A fixture written
    # with `relationship_type` directly would pass even if `plan_case` merged
    # against the raw read list instead of `current_entity_binds` -- which is
    # exactly the bug this shape catches.
    existing = {"nes_id": YADAV, "type": "accused",
                "outcome": "convicted", "notes": "hand-written by a caseworker"}
    api = _PlanApi(detail={"registration_date_ad": "2023-06-22"},
                   parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव",
                             "nes_id": YADAV}])
    plan = _plan(api, _case(entities=[existing]))
    # Same (nes_id, relationship_type) -> already present -> nothing to write.
    assert plan.entities is None


def test_a_held_defendant_does_not_block_the_case_s_other_defendants_or_dates():
    # `held` must cost only the name it names: the case's other defendant
    # still binds through the ordinary ladder, and the date fields -- which
    # `plan_case` derives from `records`, never from `rows` -- still fill. A
    # wrong implementation that let a hold short-circuit the whole case (or
    # that dropped the held name from `plan.rows` instead of reporting it)
    # would fail one of the three assertions below.
    key = normalise_name("कृष्ण प्रसाद यादव")
    held = {key: frozenset({"case-a", "case-b"})}
    api = _PlanApi(
        detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"},
                 {"side": "defendant", "name": "सिताराम यादव", "nes_id": YADAV}],
    )
    plan = _plan(api, _case(slug="case-a"), held=held)
    assert dict(plan.fields) == {"case_start_date": "2023-06-22",
                                 "case_end_date": "2024-06-04"}
    assert plan.entities == [{"nes_id": YADAV, "relationship_type": "accused",
                              "outcome": ACQUITTED,
                              "notes": "प्रतिवादी — विशेष अदालत मुद्दा 079-cr-0151"}]
    hows = {r["name"]: r["how"] for r in plan.rows}
    assert hows["कृष्ण प्रसाद यादव"] == "held"
    assert hows["सिताराम यादव"] == "nes_id"


def test_the_party_row_address_reaches_the_run_entity_key():
    # Wiring: `_accused_binds` must pass the court party row's `address`
    # through to `resolve_defendant`, or the name-plus-address key is dead code
    # at the only call site that matters and two same-named defendants on two
    # cases still collapse into one entity. Two cases, one name, two addresses,
    # one shared `run_entities` -- two keys.
    run_entities = {}
    for slug, address in (("case-a", "सर्लाही, हरिपुर-४"),
                          ("case-b", "मोरङ, विराटनगर-१२")):
        api = _PlanApi(detail={"registration_date_ad": "2023-06-22"},
                       parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव",
                                 "address": address}])
        _plan(api, _case(slug=slug), run_entities=run_entities)
    assert len(run_entities) == 2


def test_a_populated_date_is_never_overwritten():
    api = _PlanApi(detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED])
    plan = _plan(api, _case(case_start_date="2020-01-01", case_end_date="2021-01-01"))
    assert plan.fields == []


def test_a_case_with_nothing_to_change_is_a_skip():
    api = _PlanApi(detail={}, parties=[])
    plan = _plan(api, _case())
    assert plan.status == "nothing-to-do"
    assert plan.fields == [] and plan.entities is None


def test_a_case_with_no_court_reference_reports_why():
    plan = _plan(_PlanApi(), _case(court_cases=[]))
    assert plan.status == "no-court-reference"
    assert "no court reference" in plan.skips[0]


def test_a_non_draft_case_is_refused():
    plan = _plan(_PlanApi(detail={"registration_date_ad": "2023-06-22"}),
                 _case(state="PUBLISHED"))
    assert plan.status == "skip-state"


def test_a_case_payload_missing_the_entities_key_is_refused():
    # `case.get("entities") or []` cannot tell "no binds" from "this payload
    # does not carry binds at all" -- a trimmed dict from a list endpoint, say.
    # Merging against a false-empty `current` would PATCH a valid `entities`
    # list holding only the new binds, silently deleting every one the case
    # actually has. Must refuse outright rather than plan that write.
    case = _case()
    del case["entities"]
    api = _PlanApi(detail={"registration_date_ad": "2023-06-22"},
                   parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव",
                             "nes_id": YADAV}])
    plan = _plan(api, case)
    assert plan.status == "no-entities-key"
    assert plan.entities is None
    assert "entities" in plan.skips[0]


import json  # noqa: E402

import pytest  # noqa: E402

from casework.enrich_court_record import apply_plan, main  # noqa: E402


def test_apply_plan_refuses_to_write_without_an_etag():
    plan = CasePlan("case-079-cr-0151", "would-patch",
                    fields=[("case_start_date", "2023-06-22")], if_match="")
    with pytest.raises(ValueError, match="ETag"):
        apply_plan(_PlanApi(), plan)


def test_accused_binds_requires_held_explicitly():
    # No default: a caller that forgets `held` must get a loud `TypeError`,
    # not a silent "nothing is held" that reintroduces the same-name collapse
    # this task exists to stop -- the failure mode on this path is naming the
    # wrong person as accused, so a forgotten argument must fail LOUD, not open.
    with pytest.raises(TypeError):
        _accused_binds(  # ty: ignore[missing-argument] -- the point of this test
            _SearchApi(), _case(), [],
            live_prefixes=["person"], run_entities={}, dry_run=True)


def test_plan_case_requires_held_explicitly():
    with pytest.raises(TypeError):
        plan_case(  # ty: ignore[missing-argument] -- the point of this test
            _PlanApi(detail={"registration_date_ad": "2023-06-22"}),
            _case(), 'W/"7"',
            live_prefixes=["person"], run_entities={}, dry_run=True)


def test_apply_plan_sends_one_conditional_request():
    seen = {}

    class _Api:
        def patch_case(self, slug, *, fields=(), lists=(), if_match=None):
            seen.update(slug=slug, fields=list(fields), lists=list(lists),
                        if_match=if_match)
            return {}

    plan = CasePlan("case-079-cr-0151", "would-patch",
                    fields=[("case_start_date", "2023-06-22")],
                    entities=[{"nes_id": YADAV, "relationship_type": "accused"}],
                    if_match='W/"7"')
    apply_plan(_Api(), plan)
    assert seen["if_match"] == 'W/"7"'
    assert seen["fields"] == [("case_start_date", "2023-06-22")]
    assert seen["lists"][0][0] == "entities"


class _CliApi(_PlanApi):
    """`_PlanApi` plus the list/detail/write entry points `main()` calls
    before and beyond `plan_case` -- one case in, its own ETag on the read,
    and an optional canned `patch_case` outcome for the `--apply` path.
    """

    def __init__(self, case, *, etag='W/"7"', patch_error=None, **kw):
        super().__init__(**kw)
        self._case = case
        self._etag = etag
        self._patch_error = patch_error
        self.patch_calls = []

    def iter_cases(self, params=None, timeout=60, progress=None):
        yield self._case

    def get_case_with_etag(self, slug, timeout=60):
        return self._case, self._etag

    def entity_prefixes(self, timeout=60):
        return ["person"]

    def patch_case(self, slug, *, fields=(), lists=(), timeout=60, if_match=None):
        self.patch_calls.append({"slug": slug, "fields": list(fields),
                                  "lists": list(lists), "if_match": if_match})
        if self._patch_error is not None:
            raise self._patch_error
        return {}


def _events(tmp_path):
    """Every JSON line from the one `*.events.jsonl` a run leaves in `tmp_path`."""
    paths = list(tmp_path.glob("*.events.jsonl"))
    assert paths, "the run must leave an events file"
    return [json.loads(line) for line in paths[0].read_text().splitlines() if line]


def test_a_dry_run_writes_the_events_file_and_no_patch(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CASEWORK_API_USER", "dev")
    monkeypatch.setenv("CASEWORK_API_PASSWORD", "dev")
    # Stub the corpus read and the court reads; assert nothing PATCHes.
    # The defendant carries NO `nes_id` on purpose: with one, `resolve_defendant`
    # returns at ladder rung 1 and never reaches the creation rung, so the
    # `args.dry_run -> plan_case -> _accused_binds -> resolve_defendant(dry_run=...)`
    # wiring would be untested at the CLI level -- a bug that hardcoded
    # `dry_run=False` somewhere in that chain would still pass this test.
    # Dropping the nes_id forces the creation rung and lets `api.posted == []`
    # prove the CLI's `--dry-run` really reaches it and suppresses the POST.
    api = _CliApi(
        _case(),
        detail={"registration_date_ad": "2023-06-22"},
        hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}],
    )
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    events = _events(tmp_path)
    steps = {e["step"] for e in events}
    assert {"select", "court_read", "patch"} <= steps
    # No real PATCH: every `patch` event this run emits is the dry-run kind.
    assert all(e["status"] == "dry_run" for e in events if e["step"] == "patch")
    # No real POST either, even though this defendant would need a new entity
    # under `--apply`.
    assert api.posted == []
    assert api.patch_calls == []


def test_a_case_missing_the_entities_key_is_skipped_and_logged(tmp_path, monkeypatch):
    # `plan_case` refuses to plan a write off a payload with no `entities` key
    # at all -- merging would fabricate a false-empty current list and PATCH a
    # replace that deletes every bind the case actually has (see `plan_case`).
    # The CLI's job is to treat that refusal as a SKIP: no court_read, no
    # bind_plan, no patch -- and log why, the same as `skip-state` and
    # `no-court-reference` already do.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    case = _case()
    del case["entities"]
    api = _CliApi(case, detail={"registration_date_ad": "2023-06-22"})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    events = _events(tmp_path)
    assert [e["step"] for e in events] == ["select"]
    assert events[0]["status"] == "skip_no_entities_key"
    # The ONE line this case leaves must say WHY, not just THAT -- an operator
    # replaying the ledger can't otherwise tell this apart from any other
    # select-skip on the same case.
    assert "entities" in events[0]["detail"]


def test_a_non_draft_case_is_skipped_with_the_state_in_the_detail(tmp_path, monkeypatch):
    # `plan_case` already puts the actual state into `skips` for this path
    # ("state is 'PUBLISHED', not 'DRAFT'"); this pins that the CLI actually
    # surfaces it, so a `skip_state` line in the events file says WHICH state
    # rather than just that one applied.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api = _CliApi(_case(state="PUBLISHED"))
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    # `--slug` bypasses `select_for_run`'s DRAFT/IN_REVIEW gate (see
    # `casework.common.select.select_cases`) -- needed here only to get a
    # PUBLISHED case through selection so `plan_case`'s OWN state check (the
    # thing under test) is what produces the skip, not the selector dropping
    # it before `main` ever sees it.
    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--slug", "case-079-cr-0151",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    events = _events(tmp_path)
    assert [e["step"] for e in events] == ["select"]
    assert events[0]["status"] == "skip_state"
    assert "PUBLISHED" in events[0]["detail"]


def test_a_partially_unreadable_court_record_is_logged_as_court_read_not_dates(
    tmp_path, monkeypatch,
):
    # Two court references on one case; the second 404s. `court_record_for_case`
    # still returns the one successfully-read record, so the case proceeds --
    # but the skip describing the 404 must land under `court_read`/`unreadable`,
    # not `dates`: it is a fact about a broken read, not about date derivation,
    # and the case's own `court_read`/`ok` event (logged because at least one
    # reference succeeded) must not be the only word on the subject.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    second_ref = "https://jawafdehi.org/courtcase/special/079-cr-0999"

    class _TwoRefApi(_CliApi):
        def get_courtcase(self, court, number, timeout=60):
            if number == "079-cr-0999":
                raise urllib.error.HTTPError(second_ref, 404, "Not Found", {}, None)
            return super().get_courtcase(court, number, timeout=timeout)

    case = _case(court_cases=[CASE_IRI, second_ref])
    api = _TwoRefApi(case, detail={"registration_date_ad": "2023-06-22"})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    events = _events(tmp_path)

    court_read = [e for e in events if e["step"] == "court_read"]
    assert any(e["detail"] == "" for e in court_read), "the successful read"
    assert any("unreadable: " in e["detail"] and "079-cr-0999" in e["detail"]
               for e in court_read)
    # The 404 must not also (or instead) show up as a `dates` event -- only
    # the genuine date-source skip belongs there.
    dates = [e for e in events if e["step"] == "dates"]
    assert not any("079-cr-0999" in e.get("detail", "") for e in dates)
    assert any(e["detail"].startswith("no_source: ") for e in dates)
    # Both are INTERMEDIATE steps, so both report `ok` and carry the
    # classification in the detail; see `_RUNG_WORDS`. A distinctive status
    # here would be recorded by `casework.ledger` as this case's outcome.
    assert {e["status"] for e in court_read + dates} == {"ok"}


def test_a_non_prosecution_court_reference_is_logged_as_bind_plan_not_dates(
    tmp_path, monkeypatch,
):
    # `_accused_binds` skips a whole non-prosecution record without reading a
    # single party, and `_log_plan` routes that skip line under
    # `step="bind_plan"` (see `_NON_PROSECUTION_SKIP_PREFIX`), never into the
    # `dates`/`no_source` catch-all a genuine date-derivation skip uses. Task 1
    # shipped that routing branch with only a hand-run repro in its report --
    # this is the automated pin the follow-up review asked for, in the same
    # style as the court-read-failure test above.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    oa_ref = "https://jawafdehi.org/courtcase/special/079-oa-0014"
    case = _case(court_cases=[oa_ref])
    api = _CliApi(case, detail={"registration_date_ad": "2023-06-22"},
                  parties=[{"side": "defendant", "name": "कुनै व्यक्ति"}])
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    events = _events(tmp_path)

    bind_plan = [e for e in events if e["step"] == "bind_plan"]
    assert any("079-oa-0014" in e["detail"] and "not a prosecution" in e["detail"]
               for e in bind_plan)
    assert any("skipped as non-prosecution" in e["detail"] for e in bind_plan)
    # The skip must not ALSO (or instead) land under `dates`.
    dates = [e for e in events if e["step"] == "dates"]
    assert not any("079-oa-0014" in e.get("detail", "") for e in dates)
    assert {e["status"] for e in bind_plan} == {"ok"}


def test_a_held_defendant_is_logged_under_defendant_resolve_not_silently_dropped(
    tmp_path, monkeypatch,
):
    # `main()` does not build a held set yet -- a later task wires the
    # two-pass index (`defendant_name_index`/`held_names`) into it. Until
    # then this pins the `_log_plan` routing for a `how="held"` row end to
    # end by monkeypatching `plan_case` to inject a held set the same way
    # that later task will, rather than only unit-testing `_log_plan`
    # directly. Guards `_RUNG_WORDS` carrying a `"held"` entry (its absence
    # would raise `KeyError` here, not silently drop the row) and that the
    # held defendant never reaches a bind.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    case = _case()
    api = _CliApi(case, detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
                  parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}])
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)
    held = {normalise_name("कृष्ण प्रसाद यादव"): frozenset({"case-079-cr-0151", "case-b"})}
    real_plan_case = ecr.plan_case
    monkeypatch.setattr(
        ecr, "plan_case",
        lambda *a, **kw: real_plan_case(*a, **{**kw, "held": held}))

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    events = _events(tmp_path)
    resolve_events = [e for e in events if e["step"] == "defendant_resolve"]
    assert len(resolve_events) == 1
    assert resolve_events[0]["detail"].startswith("held: कृष्ण प्रसाद यादव -> ")
    assert "case-b" in resolve_events[0]["detail"]
    assert resolve_events[0]["status"] == "ok"
    assert api.posted == []
    assert api.patch_calls == []


def test_a_held_defendant_is_excluded_from_resolved_and_accused_counts(tmp_path, monkeypatch):
    # Reviewer repro: one held name plus one `nes_id`-bound defendant, dates
    # already populated. Before this fix `len(plan.rows)` counted the held
    # row as "resolved" and as part of "accused+N" too -- the bind_plan
    # summary read "2 defendant(s) resolved" and the review file's Generated
    # field read "accused+2" for a plan that only ever bound one person.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    case = _case(case_start_date="2020-01-01", case_end_date="2021-01-01")
    api = _CliApi(
        case, detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"},
                 {"side": "defendant", "name": "सिताराम यादव", "nes_id": YADAV}])
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)
    held = {normalise_name("कृष्ण प्रसाद यादव"): frozenset({"case-079-cr-0151", "case-b"})}
    real_plan_case = ecr.plan_case
    monkeypatch.setattr(
        ecr, "plan_case",
        lambda *a, **kw: real_plan_case(*a, **{**kw, "held": held}))

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0

    bind_plan = [e for e in _events(tmp_path) if e["step"] == "bind_plan"]
    assert any("1 defendant(s) resolved" in e["detail"] for e in bind_plan)
    assert not any("2 defendant(s) resolved" in e["detail"] for e in bind_plan)
    assert any("1 name(s) held for review" in e["detail"] for e in bind_plan)

    review_text = (tmp_path / "review.md").read_text(encoding="utf-8")
    assert "accused+1" in review_text
    assert "accused+2" not in review_text
    assert "1 name(s) held for review" in review_text


def test_a_held_only_nothing_to_do_case_is_not_recorded_as_already(tmp_path, monkeypatch):
    # The companion to `test_a_case_with_nothing_to_change_records_already_not_nothing`:
    # when the ONLY reason a case reaches "nothing-to-do" is a held name, the
    # stage's own work is not finished -- a human still has to rule on it.
    # `already` is excluded from nothing here: `casework.ledger.NON_OUTCOME_STATUSES`
    # does not contain it, so `build_ledger` would otherwise record this
    # stage as a COMPLETED outcome for a case whose whole point is that it
    # isn't.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    case = _case(case_start_date="2020-01-01", case_end_date="2021-01-01")
    api = _CliApi(case, detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
                  parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}])
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)
    held = {normalise_name("कृष्ण प्रसाद यादव"): frozenset({"case-079-cr-0151", "case-b"})}
    real_plan_case = ecr.plan_case
    monkeypatch.setattr(
        ecr, "plan_case",
        lambda *a, **kw: real_plan_case(*a, **{**kw, "held": held}))

    assert main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
                 "--review-file", str(tmp_path / "review.md")]) == 0
    assert api.patch_calls == []

    idempotency = [e for e in _events(tmp_path) if e["step"] == "idempotency"]
    assert len(idempotency) == 1
    assert idempotency[0]["status"] == "held_for_review"
    assert "1 name(s) held for review" in idempotency[0]["detail"]
    assert "0 court-record defendant(s) are already bound" in idempotency[0]["detail"]

    from casework.ledger import build_ledger
    status = build_ledger(tmp_path)[("case-079-cr-0151", "court_record")]["status"]
    assert status == "held_for_review"
    assert status != "already"


def test_a_dry_run_leaves_the_case_out_of_the_ledger_entirely(tmp_path, monkeypatch):
    # Fix 3, proved against a REAL run rather than a hand-written fixture: the
    # events this CLI actually emits, folded by the real
    # `casework.ledger.build_ledger`, must leave nothing behind for a dry run.
    # A dry run changed nothing, so the "what did we change, when" audit must
    # not carry a row for it -- and excluding the terminal `patch`/`dry_run`
    # status alone does not achieve that: whatever distinctive status the
    # LATEST surviving event carries becomes the outcome instead, which is how
    # `bind_plan`/`merged` was landing in the ledger for every dry-run case.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api = _CliApi(
        _case(),
        detail={"registration_date_ad": "2023-06-22"},
        hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}],
    )
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    assert main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
                 "--review-file", str(tmp_path / "review.md")]) == 0

    # The run really did emit the full sequence -- otherwise "the ledger is
    # empty" would be true for the boring reason that nothing was logged.
    steps = [e["step"] for e in _events(tmp_path)]
    assert {"select", "court_read", "defendant_resolve", "bind_plan",
            "patch"} <= set(steps)

    from casework.ledger import build_ledger
    assert build_ledger(tmp_path) == {}


def test_an_apply_run_is_recorded_in_the_ledger(tmp_path, monkeypatch):
    # The companion: "the ledger is empty" must not be achieved by excluding
    # every status this stage emits. The same sequence ending in a real PATCH
    # records `applied` against the case.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api = _CliApi(
        _case(),
        detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV}],
    )
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    assert main(["--api-base-url", "http://127.0.0.1:48010", "--apply",
                 "--review-file", str(tmp_path / "review.md")]) == 0

    from casework.ledger import build_ledger
    ledger = build_ledger(tmp_path)
    assert ledger[("case-079-cr-0151", "court_record")]["status"] == "applied"


def test_a_case_with_nothing_to_change_records_already_not_nothing(tmp_path, monkeypatch):
    # A case that needed no write ends on `ok`-statused intermediates only, so
    # without a terminal event of its own it would vanish from the ledger --
    # indistinguishable from a run that crashed before reaching it. The ledger's
    # stated value is telling "we enriched it" from "it was already populated",
    # so this path emits the sibling vocabulary for the latter.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api = _CliApi(_case(case_start_date="2020-01-01", case_end_date="2021-01-01"),
                  detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
                  parties=[])
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    assert main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
                 "--review-file", str(tmp_path / "review.md")]) == 0
    assert api.patch_calls == []

    from casework.ledger import build_ledger
    assert build_ledger(tmp_path)[("case-079-cr-0151", "court_record")]["status"] == "already"


def test_apply_run_records_a_412_as_etag_conflict_with_no_applied_event(
    tmp_path, monkeypatch, capsys,
):
    # The load-bearing chain under `--apply`: a stale read (412 on the write)
    # must record `etag_conflict`, count as an error, and emit NO `applied`
    # event -- nothing here claims a bind that never landed.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    conflict = urllib.error.HTTPError(
        "https://jawafdehi.org/api/cases/case-079-cr-0151/", 412,
        "Precondition Failed", {}, None)
    api = _CliApi(
        _case(), patch_error=conflict,
        detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV}],
    )
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--apply",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    assert api.patch_calls, "apply_plan must have actually called patch_case"
    events = _events(tmp_path)
    patch_events = [e for e in events if e["step"] == "patch"]
    assert len(patch_events) == 1
    assert patch_events[0]["status"] == "etag_conflict"
    assert not any(e["status"] == "applied" for e in patch_events)
    assert "error: 1" in capsys.readouterr().out


def test_apply_run_records_a_successful_write(tmp_path, monkeypatch):
    # The companion success path: a clean `--apply` PATCH logs `applied`,
    # carrying the merged `if_match` through to the one real write.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api = _CliApi(
        _case(),
        detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV}],
    )
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--apply",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    events = _events(tmp_path)
    patch_events = [e for e in events if e["step"] == "patch"]
    assert len(patch_events) == 1
    assert patch_events[0]["status"] == "applied"
    assert len(api.patch_calls) == 1
    assert api.patch_calls[0]["if_match"] == 'W/"7"'


def test_a_slug_containing_412_does_not_mislabel_a_missing_etag_as_a_conflict(
    tmp_path, monkeypatch,
):
    # `apply_plan`'s own no-ETag `ValueError` interpolates `plan.slug` into its
    # message. A slug that happens to contain "412" must not make a plain
    # string-search read that as an HTTP 412 -- this refusal is PERMANENT
    # (there will never be an ETag to retry with), not a transient conflict.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    case = _case(slug="case-079-cr-0412")
    api = _CliApi(case, etag="",  # no ETag at all: apply_plan refuses before any HTTP call
                  detail={"registration_date_ad": "2023-06-22"})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--apply",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    assert api.patch_calls == [], "refused before ever reaching patch_case"
    events = _events(tmp_path)
    patch_events = [e for e in events if e["step"] == "patch"]
    assert len(patch_events) == 1
    assert patch_events[0]["status"] == "rejected"


class _MultiCaseApi(_CliApi):
    """`_CliApi` serving several cases in one run, looked up by slug for the
    case detail and by court case NUMBER for the court record -- the
    two-pass / held-file tests below need more than the one canned case
    `_CliApi` alone can serve.
    """

    def __init__(self, cases, courtcase_data, *, etag='W/"7"', patch_error=None,
                fail_slugs=(), **kw):
        super().__init__(cases[0], etag=etag, patch_error=patch_error, **kw)
        self._cases = {c["slug"]: c for c in cases}
        self._courtcase_data = courtcase_data
        self._fail_slugs = set(fail_slugs)

    def iter_cases(self, params=None, timeout=60, progress=None):
        yield from self._cases.values()

    def get_case_with_etag(self, slug, timeout=60):
        if slug in self._fail_slugs:
            raise urllib.error.HTTPError(
                "https://jawafdehi.org", 500, "Internal Server Error", {}, None)
        return self._cases[slug], self._etag

    def get_courtcase(self, court, number, timeout=60):
        return self._courtcase_data[number].get("detail", {})

    def list_hearings(self, court, number, timeout=60):
        return self._courtcase_data[number].get("hearings", [])

    def get_court_case_entities(self, court, number, timeout=60):
        return self._courtcase_data[number].get("parties", [])


def test_a_pass_1_read_failure_on_one_case_does_not_stop_the_run(tmp_path, monkeypatch):
    # `case-bad`'s pass-1 `get_case_with_etag` raises. A wrong implementation
    # that let this propagate would crash `main()` before any case is
    # planned; one that caught it but stopped the pass-1 loop entirely (a
    # `return` where a `continue` belongs) would leave `case-good` never
    # planned either -- checked here by requiring `case-good` to actually
    # reach a `patch` event, not just that `main()` returns 0.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    good = _case(slug="case-good",
                court_cases=["https://jawafdehi.org/courtcase/special/079-cr-0200"])
    bad = _case(slug="case-bad",
               court_cases=["https://jawafdehi.org/courtcase/special/079-cr-0201"])
    api = _MultiCaseApi(
        [bad, good],
        {"079-cr-0200": {"detail": {"registration_date_ad": "2023-06-22"},
                        "hearings": [DECIDED],
                        "parties": [{"side": "defendant", "name": "कृष्ण प्रसाद यादव",
                                    "nes_id": YADAV}]}},
        fail_slugs=["case-bad"])
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0

    events = _events(tmp_path)
    bad_events = [e for e in events if e["slug"] == "case-bad"]
    assert len(bad_events) == 1
    assert bad_events[0]["step"] == "court_read"
    assert bad_events[0]["status"] == "unreadable"

    good_events = [e for e in events if e["slug"] == "case-good"]
    assert any(e["step"] == "patch" for e in good_events)


def test_main_holds_the_same_defendant_on_every_case_it_appears_on(tmp_path, monkeypatch):
    # The one property a previous reviewer flagged as unverifiable: every
    # case in a run must be planned against the SAME `held` mapping, built
    # from every selected case before any of them is planned. A wrong
    # implementation that built the index incrementally case-by-case (or
    # otherwise let case-a plan against a partial index) would see nothing
    # to hold when case-a is planned first, since case-b's occurrence of the
    # name has not been read yet -- so case-a would bind it, and only case-b
    # would come back "held". Both must come back "held", naming each other.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    ref_a = "https://jawafdehi.org/courtcase/special/079-cr-0151"
    ref_b = "https://jawafdehi.org/courtcase/special/080-cr-0002"
    case_a = _case(slug="case-a", court_cases=[ref_a])
    case_b = _case(slug="case-b", court_cases=[ref_b])
    shared_party = [{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}]
    api = _MultiCaseApi(
        [case_a, case_b],
        {"079-cr-0151": {"detail": {"registration_date_ad": "2023-06-22"},
                        "hearings": [DECIDED], "parties": shared_party},
         "080-cr-0002": {"detail": {"registration_date_ad": "2023-06-22"},
                        "hearings": [DECIDED], "parties": shared_party}})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0

    resolve = {e["slug"]: e for e in _events(tmp_path) if e["step"] == "defendant_resolve"}
    assert resolve["case-a"]["detail"].startswith("held: कृष्ण प्रसाद यादव -> ")
    assert resolve["case-b"]["detail"].startswith("held: कृष्ण प्रसाद यादव -> ")
    assert "case-b" in resolve["case-a"]["detail"]
    assert "case-a" in resolve["case-b"]["detail"]


def test_the_held_file_lists_a_two_case_name_and_omits_a_one_case_name(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    ref_a = "https://jawafdehi.org/courtcase/special/079-cr-0151"
    ref_b = "https://jawafdehi.org/courtcase/special/080-cr-0002"
    case_a = _case(slug="case-a", court_cases=[ref_a])
    case_b = _case(slug="case-b", court_cases=[ref_b])
    shared_name = "कृष्ण प्रसाद यादव"
    solo_name = "सिताराम यादव"
    api = _MultiCaseApi(
        [case_a, case_b],
        {"079-cr-0151": {"detail": {"registration_date_ad": "2023-06-22"},
                        "hearings": [DECIDED],
                        "parties": [{"side": "defendant", "name": shared_name},
                                   {"side": "defendant", "name": solo_name,
                                    "nes_id": YADAV}]},
         "080-cr-0002": {"detail": {"registration_date_ad": "2023-06-22"},
                        "hearings": [DECIDED],
                        "parties": [{"side": "defendant", "name": shared_name}]}})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    review_path = tmp_path / "review.md"
    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(review_path)])
    assert rc == 0

    held_path = tmp_path / "review.held.json"
    assert held_path.exists()
    payload = json.loads(held_path.read_text(encoding="utf-8"))
    by_name = {entry["name"]: entry for entry in payload["held"]}

    shared_key = normalise_name(shared_name)
    assert shared_key in by_name
    assert set(by_name[shared_key]["cases"]) == {"case-a", "case-b"}
    assert {r["slug"] for r in by_name[shared_key]["rows"]} == {"case-a", "case-b"}

    # A name on only ONE case must not appear at all -- a wrong
    # implementation that wrote every held-index candidate regardless of
    # multiplicity would still pass the assertions above but fail this one.
    assert normalise_name(solo_name) not in by_name


def test_an_applied_runs_review_row_reads_patched(tmp_path, monkeypatch):
    # Reviewer-and-smoke-test-found bug: `review.add` used to run before the
    # write was attempted, so this row read `would-patch` even under `Mode:
    # APPLIED`. A fix that keeps reading `plan.status` (always "would-patch"
    # on this path) instead of the terminal branch's own outcome would still
    # fail this.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api = _CliApi(
        _case(),
        detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV}],
    )
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    review_path = tmp_path / "review.md"
    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--apply",
               "--review-file", str(review_path)])
    assert rc == 0
    text = review_path.read_text(encoding="utf-8")
    assert "| 1 | `case-079-cr-0151` | patched |" in text
    assert "would-patch" not in text


def test_a_dry_runs_review_row_still_reads_would_patch(tmp_path, monkeypatch):
    # The companion: a dry run must NOT be relabelled `patched` by whatever
    # fixes the test above -- a wrong fix that hardcodes "patched" for every
    # would-patch plan would fail this one instead.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api = _CliApi(
        _case(),
        detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV}],
    )
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    review_path = tmp_path / "review.md"
    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(review_path)])
    assert rc == 0
    text = review_path.read_text(encoding="utf-8")
    assert "| 1 | `case-079-cr-0151` | would-patch |" in text


def test_a_failed_patchs_review_row_reads_the_failure_status(tmp_path, monkeypatch):
    # A 412 must read `etag_conflict` in the review file, not `would-patch`
    # -- an operator skimming the review file needs to see the write never
    # landed.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    conflict = urllib.error.HTTPError(
        "https://jawafdehi.org/api/cases/case-079-cr-0151/", 412,
        "Precondition Failed", {}, None)
    api = _CliApi(
        _case(), patch_error=conflict,
        detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV}],
    )
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    review_path = tmp_path / "review.md"
    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--apply",
               "--review-file", str(review_path)])
    assert rc == 0
    text = review_path.read_text(encoding="utf-8")
    assert "| 1 | `case-079-cr-0151` | etag_conflict |" in text


def test_the_module_imports_without_django(tmp_path):
    """The standalone constraint, pinned deterministically.

    Checking only `returncode == 0` proves little on its own:
    `casework.common.llm.bootstrap` (never called by this module, but the
    thing this test guards against a future edit calling) sets
    `DJANGO_SETTINGS_MODULE` itself via `os.environ.setdefault` and would
    fail closed here only because this shell has no `SECRET_KEY` -- a shell
    that exports a complete `.env` would let Django configure successfully,
    and the subprocess would exit 0 with Django fully loaded. Asserting
    `"django" not in sys.modules` INSIDE the subprocess is true regardless of
    what the environment happens to provide.
    """
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    proc = subprocess.run(
        [sys.executable, "-c",
         "import casework.enrich_court_record, sys\n"
         "loaded = sorted(m for m in sys.modules if m == 'django' or m.startswith('django.'))\n"
         "assert not loaded, loaded"],
        env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
