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


def test_a_row_nes_id_that_is_not_a_person_is_refused():
    # Rungs 2 and 3 can only ever produce a `person`, so rung 1 is the single
    # way a non-person IRI could reach an `accused` bind -- an office named as
    # the accused individual. Not a dead path: the FY078/079 cohort carries no
    # `nes_id` at all, but `special/080-cr-0111` was backfilled with 185 of them.
    api = _SearchApi()
    office = "https://jawafdehi.org/entity/organization/malpot-karyalaya-jhapa"
    got = resolve_defendant(api, "मालपोत कार्यालय झापा", office, citation="",
                            live_prefixes=["person"], run_entities={}, dry_run=True)
    assert got.nes_id == ""
    assert got.how == "failed"
    assert "not a person entity" in got.reason
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


def test_a_dry_run_admits_apply_could_refuse_this_bind():
    # `dry_run` returns the pre-POST IRI without ever attempting the create,
    # so it never reaches the `EntityAlreadyExists` handler an --apply run
    # would hit on the COMMON path for a common name: this exact slug
    # already belongs to a person the ladder declined to identify. The
    # review file this row feeds is approved BEFORE --apply runs, so the
    # reason must say a collision is possible, not imply this IRI is the
    # one that will be bound.
    api = _SearchApi(results=[])
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=["person"], run_entities={}, dry_run=True)
    assert got.how == "created"
    assert "refuse" in got.reason
    assert "--apply" in got.reason


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


from casework.common.select import ENRICHABLE_STATES  # noqa: E402
from casework.enrich_court_record import (  # noqa: E402
    ACQUITTED,
    CHARGED,
    REQUIRED_WRITE_STATE,
    CasePlan,
    accused_table,
    bind_outcome,
    court_read_summary,
    plan_case,
    rung_summary,
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


def test_the_new_entity_citation_comes_from_the_first_bindable_record():
    # Reviewer repro: `citation` used to read `records[0]` before the
    # per-record filter ran, so a CR defendant's new entity could be cited
    # to a writ (`OA`) material that never names them -- a false provenance
    # claim on a public NES record. The OA reference is listed FIRST here on
    # purpose, so only a fix that skips it when picking the citation (not
    # just when binding) can pass.
    oa_record = _record(number="079-oa-0014",
                        parties=[{"side": "defendant", "name": "कुनै व्यक्ति"}])
    oa_record["detail"]["material_id"] = "https://jawafdehi.org/material/court/special.079-oa-0014"
    cr_record = _record(number="079-cr-0151",
                        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}])
    cr_record["detail"]["material_id"] = "https://jawafdehi.org/material/court/special.079-cr-0151"
    api = _SearchApi(results=[], created={"@id": YADAV})
    items, rows, skips = _accused_binds(
        api, _case(), [oa_record, cr_record],
        live_prefixes=["person"], run_entities={}, dry_run=False, held={})
    assert [i["nes_id"] for i in items] == [YADAV]
    assert api.posted[0]["citation"] == "https://jawafdehi.org/material/court/special.079-cr-0151"


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


def test_an_in_review_case_is_selected_for_the_index_but_never_written():
    # `select_cases`'s ENRICHABLE_STATES admits IN_REVIEW, which is what the
    # held index wants -- an IN_REVIEW case's defendants are real occurrences
    # and must count toward a cross-case collision. The WRITE gate is separate
    # and narrower: `REQUIRED_WRITE_STATE` is DRAFT alone. Pinned because the
    # two are easy to conflate, and widening this one to match the selection
    # gate would start writing to cases already under human review.
    assert "IN_REVIEW" in ENRICHABLE_STATES
    assert REQUIRED_WRITE_STATE == "DRAFT"
    plan = _plan(_PlanApi(detail={"registration_date_ad": "2023-06-22"}),
                 _case(state="IN_REVIEW"))
    assert plan.status == "skip-state"
    assert "IN_REVIEW" in plan.skips[0]


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
        # Pins the two load-bearing call counts the two-pass split promises:
        # `get_case_with_etag` runs once per pass (a fresh ETag each time),
        # `get_courtcase` only in pass 1 (pass 2 reuses the cache).
        self.call_counts = {}

    def _count(self, name):
        self.call_counts[name] = self.call_counts.get(name, 0) + 1

    def iter_cases(self, params=None, timeout=60, progress=None):
        yield self._case

    def get_case_with_etag(self, slug, timeout=60):
        self._count("get_case_with_etag")
        return self._case, self._etag

    def get_courtcase(self, court, number, timeout=60):
        self._count("get_courtcase")
        return super().get_courtcase(court, number, timeout=timeout)

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


def _log_lines(tmp_path):
    """Every line from the one `*.log` a run leaves in `tmp_path`.

    Reads the rendered log file rather than `caplog`: `configure_run_logging`
    sets `propagate = False` on its logger precisely so this logger's output
    isn't doubled through root's handlers, which also means `caplog` (which
    only ever attaches to root) never sees these records.
    """
    paths = list(tmp_path.glob("*.log"))
    assert paths, "the run must leave a log file"
    return paths[0].read_text(encoding="utf-8").splitlines()


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
    # Filtered to this case's own slug: the run also emits one run-level
    # `held_index` event (slug="") for every run, which is not this case's
    # concern.
    events = [e for e in _events(tmp_path) if e["slug"] == "case-079-cr-0151"]
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
    events = [e for e in _events(tmp_path) if e["slug"] == "case-079-cr-0151"]
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
    # The successful read now states what it found -- one reference, no parties,
    # since this fixture's readable reference carries none. Still a SEPARATE
    # event from the unreadable annotation, which is what this asserts.
    assert any("court reference(s)" in e["detail"] and "unreadable" not in e["detail"]
               for e in court_read), "the successful read"
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
    # Real two-pass wiring: `case-b` names the same defendant, so `held` is
    # the genuine cross-case index `main()` builds, not an injected
    # stand-in. Guards `_RUNG_WORDS` carrying a `"held"` entry (its absence
    # would raise `KeyError` here, not silently drop the row) and that the
    # held defendant never reaches a bind.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    shared = "कृष्ण प्रसाद यादव"
    case_a = _case()
    case_b = _case(slug="case-b",
                   court_cases=["https://jawafdehi.org/courtcase/special/080-cr-0002"])
    api = _MultiCaseApi(
        [case_a, case_b],
        {"079-cr-0151": {"detail": {"registration_date_ad": "2023-06-22"},
                        "hearings": [DECIDED],
                        "parties": [{"side": "defendant", "name": shared}]},
         "080-cr-0002": {"detail": {"registration_date_ad": "2023-06-22"},
                        "hearings": [DECIDED],
                        "parties": [{"side": "defendant", "name": shared}]}})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    resolve_events = [e for e in _events(tmp_path)
                      if e["step"] == "defendant_resolve" and e["slug"] == "case-079-cr-0151"]
    assert len(resolve_events) == 1
    assert resolve_events[0]["detail"].startswith("held: कृष्ण प्रसाद यादव -> ")
    assert "case-b" in resolve_events[0]["detail"]
    assert resolve_events[0]["status"] == "ok"
    assert api.posted == []
    assert api.patch_calls == []


def test_a_dry_run_created_row_keeps_the_caveat_its_iri_would_have_hidden(
    tmp_path, monkeypatch,
):
    # `nes_id or reason` dropped the reason on every row carrying both, and
    # a dry-run "created" row is exactly that: the IRI is truthy, so the
    # warning that `--apply` refuses this bind when the slug is already taken
    # was discarded. The review file is approved BEFORE the apply, so a row
    # reading `created: <name> -> <iri>` promised a bind the run might refuse.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api = _MultiCaseApi(
        [_case()],
        {"079-cr-0151": {"detail": {"registration_date_ad": "2023-06-22"},
                         "hearings": [DECIDED],
                         "parties": [{"side": "defendant",
                                      "name": "कृष्ण प्रसाद यादव"}]}})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    created = [e for e in _events(tmp_path)
               if e["step"] == "defendant_resolve" and e["detail"].startswith("created: ")]
    assert len(created) == 1
    # Both halves: the IRI an --apply would use, AND the caveat on it.
    assert "/entity/person/" in created[0]["detail"]
    assert "--apply refuses the bind" in created[0]["detail"]
    assert api.posted == []


def test_a_held_defendant_is_excluded_from_resolved_and_accused_counts(tmp_path, monkeypatch):
    # Reviewer repro: one held name plus one `nes_id`-bound defendant, dates
    # already populated. Before this fix `len(plan.rows)` counted the held
    # row as "resolved" and as part of "accused+N" too -- the bind_plan
    # summary read "2 defendant(s) resolved" and the review file's Generated
    # field read "accused+2" for a plan that only ever bound one person.
    # `case-b` supplies the real second occurrence of the held name.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    shared = "कृष्ण प्रसाद यादव"
    case_a = _case(case_start_date="2020-01-01", case_end_date="2021-01-01")
    case_b = _case(slug="case-b", case_start_date="2020-01-01", case_end_date="2021-01-01",
                   court_cases=["https://jawafdehi.org/courtcase/special/080-cr-0002"])
    api = _MultiCaseApi(
        [case_a, case_b],
        {"079-cr-0151": {"detail": {"registration_date_ad": "2023-06-22"},
                        "hearings": [DECIDED],
                        "parties": [{"side": "defendant", "name": shared},
                                   {"side": "defendant", "name": "सिताराम यादव",
                                    "nes_id": YADAV}]},
         "080-cr-0002": {"detail": {"registration_date_ad": "2023-06-22"},
                        "hearings": [DECIDED],
                        "parties": [{"side": "defendant", "name": shared}]}})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0

    bind_plan = [e for e in _events(tmp_path)
                if e["step"] == "bind_plan" and e["slug"] == "case-079-cr-0151"]
    assert any("1 defendant(s) resolved" in e["detail"] for e in bind_plan)
    assert not any("2 defendant(s) resolved" in e["detail"] for e in bind_plan)
    assert any("1 name(s) held for review" in e["detail"] for e in bind_plan)

    review_text = (tmp_path / "review.md").read_text(encoding="utf-8")
    assert "accused+1" in review_text
    assert "accused+2" not in review_text
    assert "1 name(s) held for review" in review_text


def test_the_review_file_names_the_accused_and_states_the_outcome(tmp_path, monkeypatch):
    # The reason this exists: a reviewer reading a dry run saw `52 chars -> 63
    # chars` and `accused+2`, and could not check a single name or verdict --
    # which is the whole thing this stage is meant to be reviewed for.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api = _CliApi(
        _case(),
        detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV},
                 {"side": "defendant", "name": "सिताराम यादव"}],
    )
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    text = (tmp_path / "review.md").read_text(encoding="utf-8")
    assert "### Court-record defendants" in text
    assert "कृष्ण प्रसाद यादव" in text
    assert "सिताराम यादव" in text
    # DECIDED is a plain सफाई on the only reference, so the case acquits -- and
    # the summary line has to say so, not just count the binds.
    assert f"accused+2 ({ACQUITTED})" in text
    assert ACQUITTED in text.split("### Court-record defendants")[1]


def test_a_failed_resolution_is_not_counted_as_resolved_or_bound(tmp_path, monkeypatch):
    # Reviewer repro, verbatim: parties `["कृष्ण प्रसाद यादव", "!!!", "???"]`
    # produce exactly ONE bind item (the `nes_id` copy), but the old
    # `resolved_count = len(plan.rows) - held_count` reported "2
    # defendant(s) resolved" and `accused+2` -- it counted the `how="failed"`
    # row (an unslugabble punctuation-only name) as resolved. `"!!!"` and
    # `"???"` both normalise to "" (`normalise_name` strips all punctuation),
    # so the per-case dedup in `_accused_binds` collapses them to ONE row --
    # which is exactly why the real repro used two different symbols and
    # still only produced a single extra row to miscount.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api = _CliApi(
        _case(),
        detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV},
                 {"side": "defendant", "name": "!!!"},
                 {"side": "defendant", "name": "???"}],
    )
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0

    bind_plan = [e for e in _events(tmp_path) if e["step"] == "bind_plan"]
    assert any("1 defendant(s) resolved" in e["detail"] for e in bind_plan)
    assert not any("2 defendant(s) resolved" in e["detail"] for e in bind_plan)

    review_text = (tmp_path / "review.md").read_text(encoding="utf-8")
    assert "accused+1" in review_text
    assert "accused+2" not in review_text


def test_a_held_only_nothing_to_do_case_is_not_recorded_as_already(tmp_path, monkeypatch):
    # The companion to `test_a_case_with_nothing_to_change_records_already_not_nothing`:
    # when the ONLY reason a case reaches "nothing-to-do" is a held name, the
    # stage's own work is not finished -- a human still has to rule on it.
    # `already` is excluded from nothing here: `casework.ledger.NON_OUTCOME_STATUSES`
    # does not contain it, so `build_ledger` would otherwise record this
    # stage as a COMPLETED outcome for a case whose whole point is that it
    # isn't. `case-b` supplies the real second occurrence of the held name.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    shared = "कृष्ण प्रसाद यादव"
    case_a = _case(case_start_date="2020-01-01", case_end_date="2021-01-01")
    case_b = _case(slug="case-b", case_start_date="2020-01-01", case_end_date="2021-01-01",
                   court_cases=["https://jawafdehi.org/courtcase/special/080-cr-0002"])
    api = _MultiCaseApi(
        [case_a, case_b],
        {"079-cr-0151": {"detail": {"registration_date_ad": "2023-06-22"},
                        "hearings": [DECIDED],
                        "parties": [{"side": "defendant", "name": shared}]},
         "080-cr-0002": {"detail": {"registration_date_ad": "2023-06-22"},
                        "hearings": [DECIDED],
                        "parties": [{"side": "defendant", "name": shared}]}})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    assert main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
                 "--review-file", str(tmp_path / "review.md")]) == 0
    assert api.patch_calls == []

    idempotency = [e for e in _events(tmp_path)
                  if e["step"] == "idempotency" and e["slug"] == "case-079-cr-0151"]
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
    """`_CliApi` serving several cases at once, keyed by slug and by court case number."""

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


class _EmptySlugApi:
    """Two cases, deliberately both slug-less. `_MultiCaseApi` cannot express
    this fixture at all -- it keys its own case map by slug, so two cases
    sharing `""` would collide there first."""

    def __init__(self, cases):
        self._cases = cases

    def iter_cases(self, params=None, timeout=60, progress=None):
        yield from self._cases

    def get_case_with_etag(self, slug, timeout=60):
        raise AssertionError("a slug-less case must never reach a case read")

    def get_courtcase(self, court, number, timeout=60):
        raise AssertionError("a slug-less case must never reach a court read")

    def list_hearings(self, court, number, timeout=60):
        raise AssertionError("a slug-less case must never reach a court read")

    def get_court_case_entities(self, court, number, timeout=60):
        raise AssertionError("a slug-less case must never reach a court read")

    def entity_prefixes(self, timeout=60):
        return ["person"]

    def patch_case(self, slug, *, fields=(), lists=(), timeout=60, if_match=None):
        raise AssertionError("a slug-less case must never reach a patch")


def test_two_slug_less_cases_do_not_collide_on_an_empty_key(tmp_path, monkeypatch):
    # Reviewer repro: pass 1 did `slug = case.get("slug") or ""` with no
    # guard, so two slug-less cases both keyed `court_records[""]`, both
    # entered `readable_cases`, and pass 2 planned BOTH against whichever
    # record set pass 1 wrote there last -- case B's court record reaching
    # case A's `_accused_binds`. Each stub method below raises if pass 1
    # ever gets far enough to call it, so this fails loudly rather than
    # quietly proving nothing if the guard regresses.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    case_a = {"state": "DRAFT",
             "court_cases": ["https://jawafdehi.org/courtcase/special/079-cr-0151"],
             "entities": []}
    case_b = {"state": "DRAFT",
             "court_cases": ["https://jawafdehi.org/courtcase/special/080-cr-0002"],
             "entities": []}
    api = _EmptySlugApi([case_a, case_b])
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0

    events = _events(tmp_path)
    unreadable = [e for e in events if e["step"] == "court_read" and e["status"] == "unreadable"]
    assert len(unreadable) == 2
    assert all("no slug" in e["detail"] for e in unreadable)
    # Neither case may reach planning: no `select`, `patch`, or a resolved
    # `defendant_resolve` event of any kind should exist.
    assert not any(e["step"] in ("select", "patch", "defendant_resolve") for e in events)


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


def test_a_non_412_patch_failure_review_row_reads_rejected(tmp_path, monkeypatch):
    # The other half of the failure-status fix: a non-412 PATCH failure (a
    # 400, say) must read `rejected`, not `would-patch` and not `etag_conflict`
    # -- only 412 gets the retry-worthy label.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    bad_request = urllib.error.HTTPError(
        "https://jawafdehi.org/api/cases/case-079-cr-0151/", 400,
        "Bad Request", {}, None)
    api = _CliApi(
        _case(), patch_error=bad_request,
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
    assert "| 1 | `case-079-cr-0151` | rejected |" in text
    assert "etag_conflict" not in text
    assert "would-patch" not in text


def test_write_held_file_creates_a_review_directory_that_does_not_exist_yet(
    tmp_path, monkeypatch,
):
    # Reviewer repro: an APPLIED run whose review directory has never been
    # created (the default `work/reviews/` on a fresh worktree, or a fresh
    # `--review-file`/`CASEWORK_REVIEW_DIR` target) used to crash inside
    # `write_held_file` -- called before `review.write()`, the only thing
    # that ever `mkdir`s -- AFTER the PATCH had already landed. That would
    # lose the review file, the held file, the run footer, and the summary,
    # and exit by exception instead of returning 0.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    fresh_dir = tmp_path / "fresh-reviews"
    assert not fresh_dir.exists()
    api = _CliApi(
        _case(),
        detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV}],
    )
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--apply",
               "--review-file", str(fresh_dir / "review.md")])
    assert rc == 0
    assert api.patch_calls, "the PATCH must have actually landed before the crash point"
    assert (fresh_dir / "review.md").exists()
    assert (fresh_dir / "review.held.json").exists()


def test_the_held_index_event_reports_a_shrunk_index_after_a_pass_1_failure(
    tmp_path, monkeypatch,
):
    # Known, accepted limitation: a pass-1 failure on one case removes its
    # defendants from the index entirely, so a name it shares with a
    # SURVIVING case is no longer protected there either -- case-a binds the
    # shared name instead of holding it, because case-b's occurrence of it
    # was never read. Not fixed here (no in-run retry is in scope); this
    # pins that the run log at least SURFACES the shrink -- `selected` vs
    # `readable` in the `held_index` event -- rather than leaving `error=1`
    # in the footer as the only signal something is off.
    #
    # Review round 2 found the WARNING wired to the wrong condition: it only
    # fired for a narrowed CLI selection (`--limit`/`--slug`/etc.), never for
    # THIS scenario -- no selection flag at all, the shrink comes entirely
    # from the pass-1 read failure. A wrong fix that keeps checking only the
    # CLI flags would leave this test's WARNING assertions red.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    shared = "कृष्ण प्रसाद यादव"
    case_a = _case(slug="case-a",
                   court_cases=["https://jawafdehi.org/courtcase/special/079-cr-0151"])
    case_b = _case(slug="case-b",
                   court_cases=["https://jawafdehi.org/courtcase/special/080-cr-0002"])
    api = _MultiCaseApi(
        [case_a, case_b],
        {"079-cr-0151": {"detail": {"registration_date_ad": "2023-06-22"},
                        "hearings": [DECIDED],
                        "parties": [{"side": "defendant", "name": shared}]}},
        fail_slugs=["case-b"])
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0

    events = _events(tmp_path)
    # Documents the gap: case-a binds instead of holding, because the index
    # never saw case-b's occurrence of the shared name.
    case_a_resolve = [e for e in events
                      if e["step"] == "defendant_resolve" and e["slug"] == "case-a"]
    assert case_a_resolve and case_a_resolve[0]["detail"].startswith("created: ")

    index_events = [e for e in events if e["step"] == "held_index"]
    assert len(index_events) == 1
    assert "selected=2" in index_events[0]["detail"]
    assert "readable=1" in index_events[0]["detail"]
    # No selection flag narrowed this run -- the WARNING must still fire,
    # and must name the read failure, not the (absent) CLI-flag reason.
    assert "WARNING" in index_events[0]["detail"]
    assert "pass-1 read" in index_events[0]["detail"]
    assert "--limit" not in index_events[0]["detail"]

    # The marker must be visible to level-based filtering too, not only to
    # someone grepping `detail` -- `log_event` takes a `level` kwarg for
    # exactly this, and the rendered log line must actually carry it.
    held_index_lines = [ln for ln in _log_lines(tmp_path) if "step=held_index" in ln]
    assert held_index_lines
    assert " WARNING [" in held_index_lines[0]
    assert " INFO " not in held_index_lines[0]


def test_the_held_index_event_warns_when_the_selection_is_narrowed(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    case_a = _case(slug="case-a",
                   court_cases=["https://jawafdehi.org/courtcase/special/079-cr-0151"])
    case_b = _case(slug="case-b",
                   court_cases=["https://jawafdehi.org/courtcase/special/080-cr-0002"])
    api = _MultiCaseApi(
        [case_a, case_b],
        {"079-cr-0151": {"detail": {"registration_date_ad": "2023-06-22"}},
         "080-cr-0002": {"detail": {"registration_date_ad": "2023-06-22"}}})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--limit", "1", "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    index_events = [e for e in _events(tmp_path) if e["step"] == "held_index"]
    assert len(index_events) == 1
    assert "WARNING" in index_events[0]["detail"]
    assert "selected=1" in index_events[0]["detail"]


def test_the_held_index_event_has_no_warning_for_an_unrestricted_run(tmp_path, monkeypatch):
    # The companion: a wrong implementation that always prints the warning
    # (regardless of selection) would still pass the test above but fail
    # this one -- a plain bulk sweep over both cases, no --limit/--slug/
    # --court-case/--batch-csv, must not carry it.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    case_a = _case(slug="case-a",
                   court_cases=["https://jawafdehi.org/courtcase/special/079-cr-0151"])
    case_b = _case(slug="case-b",
                   court_cases=["https://jawafdehi.org/courtcase/special/080-cr-0002"])
    api = _MultiCaseApi(
        [case_a, case_b],
        {"079-cr-0151": {"detail": {"registration_date_ad": "2023-06-22"}},
         "080-cr-0002": {"detail": {"registration_date_ad": "2023-06-22"}}})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    index_events = [e for e in _events(tmp_path) if e["step"] == "held_index"]
    assert len(index_events) == 1
    assert "WARNING" not in index_events[0]["detail"]
    assert "selected=2" in index_events[0]["detail"]


def test_the_held_index_event_states_it_cannot_see_published_binds(
    tmp_path, monkeypatch,
):
    # Reviewer repro: bulk selection filters to `ENRICHABLE_STATES`, so a
    # PUBLISHED case's confirmed accused binds are absent from the index
    # unconditionally -- not because of `--limit`/a pass-1 failure, so this
    # must appear even on a clean, unrestricted, un-shrunk run where neither
    # WARNING branch fires. Before this fix the line said nothing about it,
    # which reads as "the index is complete" when it structurally cannot be.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    case_a = _case(slug="case-a")
    api = _MultiCaseApi(
        [case_a],
        {"079-cr-0151": {"detail": {"registration_date_ad": "2023-06-22"}}})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    index_events = [e for e in _events(tmp_path) if e["step"] == "held_index"]
    assert len(index_events) == 1
    assert "WARNING" not in index_events[0]["detail"]
    assert "DRAFT" in index_events[0]["detail"] and "IN_REVIEW" in index_events[0]["detail"]
    assert "PUBLISHED" in index_events[0]["detail"]


def test_the_held_index_event_warns_when_fiscal_year_narrows_the_selection(
    tmp_path, monkeypatch,
):
    # Reviewer repro: `narrowed` checked `--limit`/`--slug`/`--court-case`/
    # `--batch-csv` but not `--fiscal-year` -- the selector an operator
    # scoping a whole campaign is most likely to use. Two DRAFT cases in
    # different fiscal years; `--fiscal-year 79` selects only one, so
    # `selected=1` with nothing failing its pass-1 read (`shrunk` stays
    # False) -- only the `narrowed` branch can produce this WARNING.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    case_a = _case(slug="case-a",
                   court_cases=["https://jawafdehi.org/courtcase/special/079-cr-0151"])
    case_b = _case(slug="case-b",
                   court_cases=["https://jawafdehi.org/courtcase/special/080-cr-0002"])
    api = _MultiCaseApi(
        [case_a, case_b],
        {"079-cr-0151": {"detail": {"registration_date_ad": "2023-06-22"}},
         "080-cr-0002": {"detail": {"registration_date_ad": "2023-06-22"}}})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--fiscal-year", "79", "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    index_events = [e for e in _events(tmp_path) if e["step"] == "held_index"]
    assert len(index_events) == 1
    assert "selected=1" in index_events[0]["detail"]
    assert "readable=1" in index_events[0]["detail"]
    assert "WARNING" in index_events[0]["detail"]
    assert "--fiscal-year" in index_events[0]["detail"]


def test_pass_2_reuses_the_cached_court_record_but_gets_a_fresh_etag(tmp_path, monkeypatch):
    # The two load-bearing properties the brief warns against silently
    # regressing: `get_case_with_etag` must fire ONCE PER PASS (a fresh ETag
    # each time -- "optimising away the second read" would drop this to 1),
    # and `get_courtcase` must fire only in pass 1 (dropping `court_record=`
    # and re-reading in pass 2 would push this to 2).
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
    assert api.call_counts["get_case_with_etag"] == 2
    assert api.call_counts["get_courtcase"] == 1


def test_a_dry_run_does_not_re_read_a_case_for_an_etag_it_cannot_use(
    tmp_path, monkeypatch,
):
    # Pass 2's second read exists ONLY for a fresh If-Match ETag, and a dry run
    # never PATCHes -- so on --dry-run it is a wasted request per case, ~2,900
    # on a full-corpus sweep against a measured 4,470/hour budget. The --apply
    # test above pins that the re-read is still unconditional when it matters.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api = _CliApi(
        _case(),
        detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV}],
    )
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    assert api.call_counts["get_case_with_etag"] == 1
    assert api.call_counts["get_courtcase"] == 1
    assert api.patch_calls == []


def test_the_module_imports_without_django(tmp_path):
    """The standalone constraint, pinned deterministically.

    Checking only `returncode == 0` proves little on its own:
    `casework.common.llm.bootstrap` -- which `main` DOES call, but only inside
    the held-name comparison branch, never at import -- sets
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


# --------------------------------------------------------------------------
# The held-name comparison (`casework.held_identity`) and what the binder is
# allowed to do with its answer.
# --------------------------------------------------------------------------

from casework.enrich_court_record import (  # noqa: E402
    _held_outcome,
    bindable_defendants,
    write_held_file,
)
from casework.held_identity import CaseIdentity, HeldVerdict  # noqa: E402

SHARED = "कृष्ण प्रसाद यादव"
SHARED_KEY = normalise_name(SHARED)


@pytest.fixture(autouse=True)
def _no_live_model(monkeypatch):
    """No test in this module may reach a real provider.

    Under pytest `DJANGO_SETTINGS_MODULE` is already configured, so `main`'s
    `bootstrap()` and `from llm.invoke import invoke_json` both SUCCEED here --
    every CLI test that produces a held name would otherwise spend a real
    premium call. Returning `{}` means "no verdict for any name", which is
    precisely the pre-comparison behaviour those tests already assert, so this
    stub changes no existing expectation. Tests about the comparison itself
    override it; `test_the_comparison_sweep_runs_unless_it_is_turned_off` pins
    that the sweep is genuinely reached, so this fixture cannot hide a removed
    or broken sweep.
    """
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "compare_held", lambda *a, **kw: {})


def _verdict(**over):
    base = {"verdict": "different", "confidence": "high",
            "evidence": ("Rautahat elected ward chair versus Jhapa contracted "
                         "environment officer -- different districts and posts."),
            "per_case": {"case-a": "वडा अध्यक्ष, रौतहट",
                         "case-b": "वातावरण अधिकृत, झापा"}}
    base.update(over)
    return HeldVerdict(**base)


def _identity(slug, *, districts=(), court_cases=()):
    return CaseIdentity(slug=slug, districts=tuple(districts),
                        court_cases=tuple(court_cases))


def _binds(case_slug, *, held, decisions=None, identity=None, api=None,
           run_entities=None, parties=None, dry_run=True):
    record = _record(parties=parties or [{"side": "defendant", "name": SHARED}])
    return _accused_binds(
        api or _SearchApi(), _case(slug=case_slug), [record],
        live_prefixes=["person"],
        run_entities={} if run_entities is None else run_entities,
        dry_run=dry_run, held=held, decisions=decisions, identity=identity)


class _SlugStoreApi(_SearchApi):
    """One slug, one entity -- what the server actually enforces.

    Needed for any test about entity SHARING: under `--dry-run`
    `resolve_defendant` derives the IRI from the slug and never posts, so two
    cases resolving the same name produce the same IRI whether or not they
    shared a `run_entities` entry. Only a real create can tell reuse (one POST,
    both bound) from a collision (two POSTs, the second binding nothing).
    """

    def create_entity(self, payload, timeout=60):
        taken = {p["slug"] for p in self.posted}
        self.posted.append(payload)
        iri = build_entity_iri(PERSON_PREFIX, payload["slug"])
        if payload["slug"] in taken:
            raise EntityAlreadyExists(iri)
        return {"@id": iri}


# ------------------------------------------------- the hold, verdict by verdict

def test_no_verdict_at_all_leaves_a_held_name_held():
    # The regression guard for the whole feature: an absent verdict must behave
    # exactly as the binder did before the comparison existed.
    items, rows, _ = _binds("case-a",
                            held={SHARED_KEY: frozenset({"case-a", "case-b"})})
    assert items == []
    assert rows[0]["how"] == "held"
    assert "held for a human to rule on" in rows[0]["reason"]


@pytest.mark.parametrize("over", [
    {"confidence": "medium"},
    {"verdict": "unclear"},
    {"evidence": "छोटो"},
    {"per_case": {}},
])
def test_a_verdict_short_of_the_bar_leaves_the_name_held(over):
    held = {SHARED_KEY: frozenset({"case-a", "case-b"})}
    items, rows, _ = _binds("case-a", held=held,
                            decisions={SHARED_KEY: _verdict(**over)})
    assert items == []
    assert rows[0]["how"] == "held"


def test_a_held_row_quotes_what_the_model_actually_said():
    # The operator must see WHY it is still held: "unclear/low" and "the model
    # never answered" call for different follow-up.
    held = {SHARED_KEY: frozenset({"case-a", "case-b"})}
    items, rows, _ = _binds(
        "case-a", held=held,
        decisions={SHARED_KEY: _verdict(verdict="unclear", confidence="low",
                                        evidence="both cases name a मालपोत office")})
    assert items == []
    assert "unclear/low" in rows[0]["reason"]
    assert "मालपोत" in rows[0]["reason"]


def test_a_failed_comparison_is_reported_as_the_model_not_answering():
    held = {SHARED_KEY: frozenset({"case-a", "case-b"})}
    _, rows, _ = _binds("case-a", held=held,
                        decisions={SHARED_KEY: _verdict(failed=True)})
    assert "the model did not answer" in rows[0]["reason"]


# ------------------------------------------------------------ different: split

def test_a_different_verdict_gives_each_case_its_own_entity():
    # Two real creates, two distinct slugs, neither colliding -- the split has
    # to survive a server that enforces one slug per entity, not just produce
    # two different strings in a dry run.
    held = {SHARED_KEY: frozenset({"case-a", "case-b"})}
    decisions = {SHARED_KEY: _verdict()}
    api, run_entities = _SlugStoreApi(), {}
    items_a, rows_a, _ = _binds("case-a", held=held, decisions=decisions, api=api,
                                identity=_identity("case-a", districts=["rautahat"]),
                                run_entities=run_entities, dry_run=False)
    items_b, rows_b, _ = _binds("case-b", held=held, decisions=decisions, api=api,
                                identity=_identity("case-b", districts=["jhapa"]),
                                run_entities=run_entities, dry_run=False)
    assert [p["slug"] for p in api.posted] == ["krishna-prasada-yadava-rautahat",
                                               "krishna-prasada-yadava-jhapa"]
    assert items_a[0]["nes_id"].endswith("-rautahat")
    assert items_b[0]["nes_id"].endswith("-jhapa")
    assert rows_a[0]["how"] == "created" and rows_b[0]["how"] == "created"


def test_a_different_verdict_refuses_the_one_existing_namesake_entity():
    # Ladder rung 2 binds the single person entity carrying this name. The
    # verdict has just said these cases name two people, so at most one of them
    # IS that entity and nothing here can say which -- matching would hand both
    # cases the same IRI, the merge the split exists to prevent.
    api = _SearchApi(results=[_hit(YADAV, SHARED)], complete=True)
    held = {SHARED_KEY: frozenset({"case-a", "case-b"})}
    items, rows, _ = _binds("case-a", held=held, decisions={SHARED_KEY: _verdict()},
                            identity=_identity("case-a", districts=["rautahat"]),
                            api=api)
    assert items[0]["nes_id"] != YADAV
    assert items[0]["nes_id"].endswith("-rautahat")
    assert rows[0]["how"] == "created"
    # The verdict itself is the row's record of why the match was passed over.
    assert rows[0]["reason"].startswith("held verdict different/high")


def test_two_split_cases_in_one_district_never_share_an_entity():
    """Review finding 1: the split silently became the merge it prevents.

    Both cases bound to the same single district discriminate to that district,
    so with the run cache keyed on the discriminator they computed the IDENTICAL
    key -- case-b found case-a's entry and returned "reused from this run",
    binding one entity to two people the verdict had just separated. The create's
    409 guard never ran, because the cache short-circuited it.

    `main` now refuses such a verdict up front (`splittable`); this pins the
    second line of defence, the per-case cache scope, by handing
    `_accused_binds` the verdict `main` would have downgraded.
    """
    held = {SHARED_KEY: frozenset({"case-a", "case-b"})}
    decisions = {SHARED_KEY: _verdict()}
    api, run_entities = _SlugStoreApi(), {}
    items_a, _, _ = _binds("case-a", held=held, decisions=decisions, api=api,
                           identity=_identity("case-a", districts=["jhapa"]),
                           run_entities=run_entities, dry_run=False)
    items_b, rows_b, _ = _binds("case-b", held=held, decisions=decisions, api=api,
                                identity=_identity("case-b", districts=["jhapa"]),
                                run_entities=run_entities, dry_run=False)
    # case-a binds its entity; case-b must NOT be handed the same one.
    assert items_a and items_a[0]["nes_id"].endswith("-jhapa")
    assert items_b == []
    assert rows_b[0]["how"] == "failed"
    assert "reused from this run" not in rows_b[0]["reason"]


def test_a_split_with_no_distinct_discriminator_is_downgraded_before_it_binds(
    tmp_path, monkeypatch,
):
    # The first line of defence: `main` sees both cases discriminate to the same
    # district and holds the name instead of acting on `different`.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CASEWORK_API_USER", "dev")
    monkeypatch.setenv("CASEWORK_API_PASSWORD", "dev")
    # Both cases bound to the SAME single district, so both discriminate to it.
    api = _two_case_api(entities=[
        {"nes_id": "https://jawafdehi.org/entity/location/district/jhapa-np0104"}])
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)
    monkeypatch.setattr(ecr, "compare_held", _canned_compare())
    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    events = _events(tmp_path)
    assert any("split refused" in e["detail"] for e in events
               if e["step"] == "held_compare")
    resolve = [e for e in events if e["step"] == "defendant_resolve"]
    assert resolve and all(e["detail"].startswith("held: ") for e in resolve)
    assert api.posted == []


def test_a_different_verdict_falls_back_to_the_court_case_number():
    held = {SHARED_KEY: frozenset({"case-a", "case-b"})}
    items, _, _ = _binds(
        "case-a", held=held, decisions={SHARED_KEY: _verdict()},
        identity=_identity("case-a", districts=["jhapa", "morang"],
                           court_cases=["079-CR-0071"]))
    assert items[0]["nes_id"].endswith("-079-cr-0071")


def test_a_different_verdict_with_nothing_to_separate_by_binds_nothing():
    # No single district and no court case number: both cases would derive the
    # SAME slug, so the split cannot be carried out. Reported as that, rather
    # than left to surface as a slug collision on whichever case ran second.
    held = {SHARED_KEY: frozenset({"case-a", "case-b"})}
    items, rows, _ = _binds("case-a", held=held, decisions={SHARED_KEY: _verdict()},
                            identity=_identity("case-a"))
    assert items == []
    assert rows[0]["how"] == "failed"
    assert "neither a single district nor a court case number" in rows[0]["reason"]


# ------------------------------------------------------------- same: one entity

def test_a_same_verdict_creates_the_entity_once_and_binds_it_to_both_cases():
    held = {SHARED_KEY: frozenset({"case-a", "case-b"})}
    decisions = {SHARED_KEY: _verdict(verdict="same")}
    api, run_entities = _SlugStoreApi(), {}
    items_a, _, _ = _binds("case-a", held=held, decisions=decisions, api=api,
                           run_entities=run_entities, dry_run=False)
    items_b, rows_b, _ = _binds("case-b", held=held, decisions=decisions, api=api,
                                run_entities=run_entities, dry_run=False)
    assert len(api.posted) == 1
    assert items_a[0]["nes_id"] == items_b[0]["nes_id"]
    assert rows_b[0]["reason"].startswith("held verdict same/high")


def test_a_same_verdict_shares_the_entity_even_when_one_row_carries_an_address():
    # `run_entity_key` normally keys on name AND address to keep namesakes
    # apart. A `same` verdict has replaced the address as the thing
    # establishing identity, so an address on one case's row only must not
    # split the person that verdict just merged. Without the override the two
    # cases key differently, case-b reaches the create rung, collides on the
    # slug case-a already took, and binds NOTHING.
    held = {SHARED_KEY: frozenset({"case-a", "case-b"})}
    decisions = {SHARED_KEY: _verdict(verdict="same")}
    api, run_entities = _SlugStoreApi(), {}
    items_a, _, _ = _binds(
        "case-a", held=held, decisions=decisions, api=api, dry_run=False,
        run_entities=run_entities,
        parties=[{"side": "defendant", "name": SHARED,
                  "address": "सर्लाही, हरिपुर-४"}])
    items_b, rows_b, _ = _binds("case-b", held=held, decisions=decisions, api=api,
                                run_entities=run_entities, dry_run=False)
    assert len(api.posted) == 1
    assert items_b and items_a[0]["nes_id"] == items_b[0]["nes_id"]
    assert rows_b[0]["how"] == "created"


def test_a_same_verdict_still_uses_the_one_exact_match_when_there_is_one():
    # The opposite of the `different` case: if NES holds exactly one person
    # with this name and the verdict says both cases mean one man, that entity
    # is the answer and nothing needs creating.
    api = _SearchApi(results=[_hit(YADAV, SHARED)], complete=True)
    held = {SHARED_KEY: frozenset({"case-a", "case-b"})}
    items, rows, _ = _binds("case-a", held=held,
                            decisions={SHARED_KEY: _verdict(verdict="same")},
                            api=api)
    assert items[0]["nes_id"] == YADAV
    assert rows[0]["how"] == "exact"


# ------------------------------------------------------------------ provenance

def test_an_acted_on_verdict_is_recorded_on_the_row_it_bound():
    held = {SHARED_KEY: frozenset({"case-a", "case-b"})}
    _, rows, _ = _binds("case-a", held=held, decisions={SHARED_KEY: _verdict()},
                        identity=_identity("case-a", districts=["rautahat"]))
    assert rows[0]["reason"].startswith("held verdict different/high")
    assert "Rautahat elected ward chair" in rows[0]["reason"]


def test_the_court_rows_own_nes_id_outranks_a_different_verdict():
    # Rung 1 is a pure copy of what the portal itself asserts about this row,
    # and the portal is the authority on its own records -- a model's inference
    # does not override a stated identity. This cohort carries no `nes_id` at
    # all, so the path is documented here rather than exercised in production.
    held = {SHARED_KEY: frozenset({"case-a", "case-b"})}
    items, rows, _ = _binds(
        "case-a", held=held, decisions={SHARED_KEY: _verdict()},
        identity=_identity("case-a", districts=["rautahat"]),
        parties=[{"side": "defendant", "name": SHARED, "nes_id": YADAV}])
    assert items[0]["nes_id"] == YADAV
    assert rows[0]["how"] == "nes_id"


def test_held_outcome_names_only_the_other_cases():
    reason, distinct, disc, override = _held_outcome(
        frozenset({"case-a", "case-b"}), "case-a", None, None)
    assert "case-b" in reason and "case-a" not in reason
    assert (distinct, disc, override) == (False, "", None)


# -------------------------------------------------------------- the held file

def test_the_held_file_records_the_verdict_and_whether_it_was_acted_on(tmp_path):
    records = [_record(parties=[{"side": "defendant", "name": SHARED}])]
    court_records = {"case-a": (records, []), "case-b": (records, [])}
    path = write_held_file(
        tmp_path / "review.held.json",
        {SHARED_KEY: frozenset({"case-a", "case-b"})}, court_records,
        run_id="r1", verdicts={SHARED_KEY: _verdict()})
    entry = json.loads(path.read_text(encoding="utf-8"))["held"][0]
    assert entry["comparison"]["verdict"] == "different"
    assert entry["comparison"]["acted_on"] is True
    assert entry["comparison"]["model_answered"] is True
    assert "Rautahat" in entry["comparison"]["evidence"]


def test_the_held_file_says_so_when_a_name_was_never_compared(tmp_path):
    records = [_record(parties=[{"side": "defendant", "name": SHARED}])]
    court_records = {"case-a": (records, []), "case-b": (records, [])}
    path = write_held_file(
        tmp_path / "review.held.json",
        {SHARED_KEY: frozenset({"case-a", "case-b"})}, court_records,
        run_id="r1", verdicts={})
    assert json.loads(path.read_text(encoding="utf-8"))["held"][0]["comparison"] is None


# ------------------------------------------------------------------ card input

def test_bindable_defendants_skips_a_non_prosecution_reference():
    # The identity cards must see the same defendants the index does: a
    # ministry named on an `OA` writ was never a bind candidate, so scanning
    # the case description for its name would be wasted work.
    records = [_record(number="079-OA-0004",
                       parties=[{"side": "defendant", "name": "नेपाल सरकार"}]),
               _record(parties=[{"side": "defendant", "name": SHARED}])]
    assert bindable_defendants(records) == [SHARED]


def test_bindable_defendants_de_duplicates_across_references():
    records = [_record(parties=[{"side": "defendant", "name": SHARED}]),
               _record(number="080-cr-0002",
                       parties=[{"side": "defendant", "name": SHARED}])]
    assert bindable_defendants(records) == [SHARED]


# ------------------------------------------------------------------ CLI wiring

def _canned_compare(**over):
    """A `compare_held` stand-in that honours the real one's `on_verdict` hook.

    Calling the hook matters: `main` logs each verdict through it, so a stub
    that skipped it would leave the per-name `held_compare` events untested.
    """
    def _compare(held, cards, invoke_json, *, on_verdict=None, **kw):
        verdicts = {}
        for name, slugs in sorted(held.items()):
            verdicts[name] = _verdict(**over)
            if on_verdict:
                on_verdict(name, sorted(slugs), verdicts[name])
        return verdicts
    return _compare


def _two_case_api(shared=SHARED, entities=None):
    case_a = _case(slug="case-a", entities=list(entities or []))
    case_b = _case(slug="case-b", entities=list(entities or []), court_cases=[
        "https://jawafdehi.org/courtcase/special/080-cr-0002"])
    return _MultiCaseApi(
        [case_a, case_b],
        {"079-cr-0151": {"detail": {"registration_date_ad": "2023-06-22"},
                         "hearings": [DECIDED],
                         "parties": [{"side": "defendant", "name": shared}]},
         "080-cr-0002": {"detail": {"registration_date_ad": "2023-06-22"},
                         "hearings": [DECIDED],
                         "parties": [{"side": "defendant", "name": shared}]}})


def test_the_comparison_sweep_runs_unless_it_is_turned_off(tmp_path, monkeypatch):
    # Pins that `main` genuinely reaches `compare_held` for a run with a held
    # name -- without this, the module-wide `_no_live_model` stub could hide a
    # removed sweep and every other test here would still pass.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CASEWORK_API_USER", "dev")
    monkeypatch.setenv("CASEWORK_API_PASSWORD", "dev")
    api = _two_case_api()
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)
    seen = {}

    def _fake(held, cards, invoke_json, **kw):
        seen["held"] = dict(held)
        seen["cards"] = set(cards)
        seen["tier"] = kw.get("tier")
        return {name: _verdict() for name in held}

    monkeypatch.setattr(ecr, "compare_held", _fake)
    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    assert list(seen["held"]) == [SHARED_KEY]
    assert seen["cards"] == {"case-a", "case-b"}
    assert seen["tier"] == "premium"


def test_the_fallback_discriminator_ignores_a_non_prosecution_reference(
    tmp_path, monkeypatch,
):
    # Review finding 6: `court_cases` was built from EVERY reference, so a case
    # whose first reference is an `OA` writ named its person's permanent entity
    # IRI after a court reference this binder refuses to bind from.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CASEWORK_API_USER", "dev")
    monkeypatch.setenv("CASEWORK_API_PASSWORD", "dev")
    # case-a cites an OA writ FIRST, then the CR prosecution. No district bind
    # on either case, so `discriminator` takes the court-number fallback.
    case_a = _case(slug="case-a", entities=[], court_cases=[
        "https://jawafdehi.org/courtcase/special/079-oa-0004",
        "https://jawafdehi.org/courtcase/special/079-cr-0151"])
    case_b = _case(slug="case-b", entities=[], court_cases=[
        "https://jawafdehi.org/courtcase/special/080-cr-0002"])
    ref = {"detail": {"registration_date_ad": "2023-06-22"}, "hearings": [DECIDED],
           "parties": [{"side": "defendant", "name": SHARED}]}
    api = _MultiCaseApi([case_a, case_b],
                        {"079-oa-0004": ref, "079-cr-0151": ref, "080-cr-0002": ref})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)
    monkeypatch.setattr(ecr, "compare_held", _canned_compare())
    assert main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
                 "--review-file", str(tmp_path / "review.md")]) == 0
    detail = [e["detail"] for e in _events(tmp_path)
              if e["step"] == "defendant_resolve" and e["slug"] == "case-a"][0]
    assert "-079-cr-0151" in detail
    assert "-079-oa-0004" not in detail


def test_no_held_compare_asks_no_model_and_holds_every_name(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CASEWORK_API_USER", "dev")
    monkeypatch.setenv("CASEWORK_API_PASSWORD", "dev")
    api = _two_case_api()
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    def _never(*a, **kw):
        raise AssertionError("--no-held-compare must not reach the model")

    monkeypatch.setattr(ecr, "compare_held", _never)
    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--no-held-compare", "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    resolve = [e for e in _events(tmp_path) if e["step"] == "defendant_resolve"]
    assert resolve and all(e["detail"].startswith("held: ") for e in resolve)
    assert not [e for e in _events(tmp_path) if e["step"] == "held_compare"]


def test_a_run_that_holds_nothing_never_asks_the_model(tmp_path, monkeypatch):
    # The stage's ordinary case: only 80 of ~1,414 measured defendant rows
    # carry a name that lands on two cases, so most runs must spend no tokens.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CASEWORK_API_USER", "dev")
    monkeypatch.setenv("CASEWORK_API_PASSWORD", "dev")
    api = _MultiCaseApi(
        [_case()],
        {"079-cr-0151": {"detail": {"registration_date_ad": "2023-06-22"},
                         "hearings": [DECIDED],
                         "parties": [{"side": "defendant", "name": SHARED}]}})
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    def _never(*a, **kw):
        raise AssertionError("a run with no held name must not reach the model")

    monkeypatch.setattr(ecr, "compare_held", _never)
    assert main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
                 "--review-file", str(tmp_path / "review.md")]) == 0


def test_each_held_verdict_is_logged_with_its_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CASEWORK_API_USER", "dev")
    monkeypatch.setenv("CASEWORK_API_PASSWORD", "dev")
    api = _two_case_api()
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)
    monkeypatch.setattr(ecr, "compare_held", _canned_compare())
    main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
          "--review-file", str(tmp_path / "review.md")])
    compare = [e for e in _events(tmp_path) if e["step"] == "held_compare"]
    assert any("different/high" in e["detail"] for e in compare)
    assert any("Rautahat elected ward chair" in e["detail"] for e in compare)
    assert any("1 settled by the model" in e["detail"] for e in compare)


def test_a_still_held_verdict_is_logged_as_still_held(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CASEWORK_API_USER", "dev")
    monkeypatch.setenv("CASEWORK_API_PASSWORD", "dev")
    api = _two_case_api()
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)
    monkeypatch.setattr(ecr, "compare_held",
                        _canned_compare(verdict="unclear", confidence="low"))
    main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
          "--review-file", str(tmp_path / "review.md")])
    compare = [e for e in _events(tmp_path) if e["step"] == "held_compare"]
    assert any("(still held)" in e["detail"] for e in compare)
    assert any("0 settled by the model" in e["detail"] for e in compare)


def test_the_usage_footer_survives_a_real_accumulator(tmp_path, monkeypatch, capsys):
    # Production repro, run 684a9bbf: all 25 cases planned, then `main` died in
    # the footer on `usage.totals()` -- a method of the OTHER accumulator in
    # `llm/usage.py`. The exit code went with it, so a wrapper checking rc would
    # read a fully successful dry run as a failure.
    #
    # Every other test here stubs `compare_held` without touching `usage`, so
    # `usage.calls` stayed 0 and the footer branch was never entered. This stub
    # records a call on the REAL accumulator `main` built, which is the only way
    # to reach the line that broke.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CASEWORK_API_USER", "dev")
    monkeypatch.setenv("CASEWORK_API_PASSWORD", "dev")
    api = _two_case_api()
    import casework.enrich_court_record as ecr

    def _compare(held, cards, invoke_json, *, on_verdict=None, usage=None, **kw):
        if usage is not None:
            usage.add(10, 20, provider="claude_cli", tier="premium", model="sonnet")
        return {}

    monkeypatch.setattr(ecr, "build_api", lambda args: api)
    monkeypatch.setattr(ecr, "compare_held", _compare)
    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "held-name comparison" in out
    # The lines after the table must still print -- they are how an operator
    # finds the artefacts, and the crash swallowed both.
    assert "review file:" in out
    assert "held-names file:" in out


def test_an_unavailable_provider_holds_every_name_instead_of_crashing(
    tmp_path, monkeypatch,
):
    # A provider outage must cost the held names and nothing else: the dates on
    # both cases are still planned.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("CASEWORK_API_USER", "dev")
    monkeypatch.setenv("CASEWORK_API_PASSWORD", "dev")
    api = _two_case_api()
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    def _boom(*a, **kw):
        raise RuntimeError("no provider keys")

    monkeypatch.setattr(ecr, "bootstrap", _boom)
    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    events = _events(tmp_path)
    unavailable = [e for e in events if e["step"] == "held_compare"]
    assert unavailable and unavailable[0]["status"] == "unavailable"
    assert "stays held for a human" in unavailable[0]["detail"]
    assert [e for e in events if e["step"] == "dates"]


# --------------------------------------------------- the `भन्ने` alias, end to end

ALIAS_RAW = "आवास भन्ने आभाश अर्याल"
ALIAS_LEGAL = "आभाश अर्याल"


def test_the_bind_uses_the_legal_name_not_the_raw_court_string():
    # Run b2f31c03 created `person/avasa-bhanne-abhasha-aryala` for this row --
    # a name the man holds nowhere. The entity must be built from the legal name.
    api = _SearchApi(results=[], created={"@id": YADAV})
    items, rows, _ = _accused_binds(
        api, _case(), [_record(parties=[{"side": "defendant", "name": ALIAS_RAW}])],
        live_prefixes=["person"], run_entities={}, dry_run=False, held={})
    assert api.posted[0]["name"] == ALIAS_LEGAL
    assert rows[0]["name"] == ALIAS_LEGAL
    assert rows[0]["aliases"] == ["आवास"]
    assert len(items) == 1


def test_the_stripped_alias_is_recorded_on_the_bind_note():
    # The court called this person something. Dropping it loses the only place
    # the record says so, so it rides on the bind rather than the entity name.
    api = _SearchApi(results=[], created={"@id": YADAV})
    items, _, _ = _accused_binds(
        api, _case(), [_record(parties=[{"side": "defendant", "name": ALIAS_RAW}])],
        live_prefixes=["person"], run_entities={}, dry_run=False, held={})
    assert ALIAS_RAW in items[0]["notes"]
    assert "प्रतिवादी" in items[0]["notes"]


def test_the_dry_run_slug_no_longer_carries_the_alias_marker():
    got = resolve_defendant(_SearchApi(results=[]), ALIAS_LEGAL, None, citation="",
                            live_prefixes=["person"], run_entities={}, dry_run=True)
    assert "bhanne" not in got.nes_id
    assert got.nes_id.endswith("abhasha-aryala")


def test_the_held_index_keys_the_alias_form_and_the_plain_form_together():
    # One man, two cases: one record writes the alias form, the other the plain
    # name. Keyed on the raw string these are two buckets, so the hold that
    # exists to stop a wrong bind never fires for him.
    index = defendant_name_index({
        "case-a": [_record(parties=[{"side": "defendant", "name": ALIAS_RAW}])],
        "case-b": [_record(number="080-cr-0002",
                           parties=[{"side": "defendant", "name": ALIAS_LEGAL}])],
    })
    assert index == {normalise_name(ALIAS_LEGAL): frozenset({"case-a", "case-b"})}
    assert held_names(index)


def test_bindable_defendants_reports_the_legal_name():
    records = [_record(parties=[{"side": "defendant", "name": ALIAS_RAW}])]
    assert bindable_defendants(records) == [ALIAS_LEGAL]


def test_the_accused_table_shows_the_alias_beside_the_legal_name():
    table = accused_table([_row(name=ALIAS_LEGAL, aliases=["आवास"])])
    assert ALIAS_LEGAL in table
    assert "भन्ने: आवास" in table


# --------------------------------------------------------------- run-level tally

def test_the_rung_tally_prints_every_rung_including_its_zeroes():
    # `0 matched` is the number worth reading in this corpus: 142 of 142
    # defendants in the 25-case run were created, not matched. A tally that
    # dropped zeroes would hide it.
    rows = [_row(how="created"), _row(how="created"), _row(how="exact")]
    assert rung_summary(rows) == ("3 defendant(s): 0 copied, 1 matched, "
                                 "2 created, 0 held, 0 failed")


def test_the_rung_tally_of_no_defendants_still_reads():
    assert rung_summary([]) == ("0 defendant(s): 0 copied, 0 matched, "
                               "0 created, 0 held, 0 failed")


def test_the_rung_tally_counts_held_and_failed_rows_separately():
    rows = [_row(how="held", nes_id=""), _row(how="failed", nes_id=""),
            _row(how="nes_id")]
    assert rung_summary(rows) == ("3 defendant(s): 1 copied, 0 matched, "
                                 "0 created, 1 held, 1 failed")


def test_the_court_read_summary_counts_references_parties_and_defendants():
    records = [_record(parties=[{"side": "defendant", "name": "क"},
                                {"side": "plaintiff", "name": "नेपाल सरकार"}]),
               _record(number="080-cr-0002",
                       parties=[{"side": "defendant", "name": "ख"}])]
    assert court_read_summary(records) == ("2 court reference(s), 3 part(ies), "
                                          "2 defendant(s)")


# ----------------------------------------------- the review file's accused table

def _row(**over):
    base = {"slug": "case-1", "name": "कृष्ण प्रसाद यादव", "how": "created",
            "nes_id": YADAV, "outcome": CHARGED, "reason": "", "aliases": [],
            "court_case": "special/079-cr-0151"}
    base.update(over)
    return base


def test_the_accused_table_names_every_defendant_with_its_outcome():
    # The gap this closes: `generated` says `accused+21` and the summary table
    # counts characters, so before this a reviewer could not see WHO would be
    # bound or WHAT verdict the bind claims.
    table = accused_table([_row(), _row(name="सीता देवी पौडेल", how="exact")])
    assert "कृष्ण प्रसाद यादव" in table
    assert "सीता देवी पौडेल" in table
    assert table.count(CHARGED) == 2
    assert "created" in table and "exact_match" in table


def test_an_unbound_defendant_shows_no_outcome():
    # A held or failed name writes no bind, so printing the case's outcome next
    # to it would claim a verdict was recorded for someone who was never bound.
    table = accused_table([_row(how="held", nes_id="", reason="also on case-b")])
    assert CHARGED not in table
    assert "also on case-b" in table
    assert "| — |" in table


def test_the_accused_table_escapes_a_pipe_in_a_court_record_name():
    # Court-record names are portal free text. An unescaped `|` shifts every
    # column after it and the row a caseworker must act on becomes unreadable.
    table = accused_table([_row(name="यादव | समेत")])
    assert r"यादव \| समेत" in table


def test_a_case_with_no_defendants_gets_no_table():
    assert accused_table([]) == ""


# --------------------------------------------------------- many accused per case

def test_a_case_with_several_defendants_binds_every_one_in_order():
    # The production shape: 142 binds across 25 cases, 22 of them carrying more
    # than one defendant and the largest carrying 27. Every defendant needs its
    # own bind, in court-record order, each `accused` and each carrying the
    # case's outcome.
    ids = [build_entity_iri(PERSON_PREFIX, f"defendant-{n}") for n in range(1, 6)]
    names = ["राम बहादुर थापा", "सीता देवी पौडेल", "हरि प्रसाद शर्मा",
             "गीता कुमारी राई", "बिनोद कुमार यादव"]
    parties = [{"side": "defendant", "name": name, "nes_id": iri}
               for name, iri in zip(names, ids, strict=True)]
    api = _PlanApi(detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
                   parties=[*parties, {"side": "plaintiff", "name": "नेपाल सरकार"}])
    plan = _plan(api, _case())
    assert [i["nes_id"] for i in plan.entities] == ids
    assert {i["relationship_type"] for i in plan.entities} == {"accused"}
    assert {i["outcome"] for i in plan.entities} == {ACQUITTED}
    assert [r["name"] for r in plan.rows] == names


def test_defendants_on_one_case_settle_on_different_rungs_independently():
    # The ladder is per-DEFENDANT, not per-case: one row carries its own
    # `nes_id` (rung 1), one matches an existing NES person exactly (rung 2),
    # and one matches nothing and is created (rung 3) -- all on one case. An
    # implementation that picked a rung per case would flatten these to one
    # `how`, and the 27th defendant on a case would inherit the 1st one's fate.
    matched = build_entity_iri(PERSON_PREFIX, "sita-devi-poudel")
    parties = [{"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV},
               {"side": "defendant", "name": "सीता देवी पौडेल"},
               {"side": "defendant", "name": "हरि प्रसाद शर्मा"}]
    # One search stub serves all three names, and only सीता देवी पौडेल matches it
    # exactly -- so हरि प्रसाद शर्मा falls through to rung 3 for the right reason.
    api = _SearchApi(results=[_hit(matched, "सीता देवी पौडेल")], complete=True)
    items, rows, _ = _accused_binds(
        api, _case(), [_record(parties=parties)],
        live_prefixes=["person"], run_entities={}, dry_run=True, held={})
    assert [r["how"] for r in rows] == ["nes_id", "exact", "created"]
    assert [i["nes_id"] for i in items][:2] == [YADAV, matched]
    assert len(items) == 3


def test_one_defendant_named_on_two_references_of_one_case_binds_once():
    # De-duplication is by normalised NAME across every reference on the case.
    # Without it a person named on both references of a two-reference case
    # would get two identical binds in the same PATCH body.
    party = {"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV}
    records = [_record(number="079-cr-0151", parties=[party]),
               _record(number="080-cr-0002", parties=[dict(party)])]
    items, rows, _ = _accused_binds(
        _SearchApi(), _case(), records,
        live_prefixes=["person"], run_entities={}, dry_run=True, held={})
    assert [i["nes_id"] for i in items] == [YADAV]
    assert len(rows) == 1


# ------------------------------------------------------- the outcome vocabulary

#: Real deciding-hearing `decision_type` cells and the outcome each must earn.
#: `convicted` and `abated` are legal values of the model field
#: (`cases.models.CaseEntityRelationship.Outcome`) and this stage emits NEITHER
#: -- see `bind_outcome`. A court cell states what happened to the CASE; only a
#: whole-case acquittal distributes to each defendant unchanged.
OUTCOME_CELLS = [
    ("सफाई", ACQUITTED),           # plain acquittal -- the one non-default
    ("ठहर", CHARGED),              # conviction: does not say WHICH defendant
    ("आंशिक ठहर", CHARGED),        # some convicted, some cleared
    ("आंशिक सफाई", CHARGED),       # qualified acquittal
    ("तामेली", CHARGED),           # struck off / abated
    ("खारेज", CHARGED),            # quashed
    ("मुद्दा खारेज", CHARGED),
]


def test_the_stage_emits_only_charged_or_acquitted_never_convicted_or_abated():
    # `validate_bind_item` checks that `outcome` is legal only on an `accused`
    # bind; it does NOT check the value against the field's choices. So this is
    # the only gate standing between a decision cell and the request body.
    for cell, want in OUTCOME_CELLS:
        hearing = {**DECIDED, "decision_type": cell}
        got = bind_outcome([_record(hearings=[hearing])])
        assert got == want, f"{cell!r} produced {got!r}, wanted {want!r}"
        assert got in (CHARGED, ACQUITTED)


def test_an_abated_reference_is_charged_and_earns_no_end_date():
    # `तामेली` is how this corpus spells struck-off/abated. It reaches the
    # binder two ways and both must stay conservative. As a `decision_type` on
    # a decided row the case still ends (फैसला names a verdict) but the
    # defendants stay CHARGED, never ABATED. As the reference's whole
    # `case_status` it is not a verdict at all: `parse_case_status` extracts no
    # date from it, so the case gets NO end date rather than a guessed one.
    as_decision = _record(reg="2023-06-22", hearings=[{**DECIDED, "decision_type": "तामेली"}])
    assert bind_outcome([as_decision]) == CHARGED
    assert end_date([as_decision])[0] == "2024-06-04"

    as_status = _record(reg="2023-06-22", status="तामेली")
    assert bind_outcome([as_status]) == CHARGED
    value, reason = end_date([as_status])
    assert value == ""
    assert reason


def test_an_abated_reference_mixed_with_an_acquittal_is_charged():
    # The `all(acquitted)` rule has to hold for abatement too: one struck-off
    # reference must stop the other reference's सफाई from acquitting the case.
    records = [_record(hearings=[DECIDED]),
               _record(hearings=[{**DECIDED, "decision_type": "तामेली"}])]
    assert bind_outcome(records) == CHARGED


# -------------------------------------------------- a mis-typed existing accused

RELATED_ROLES = ["related", "witness", "alleged", "victim", "respondent"]


def test_an_existing_wrong_typed_bind_gains_an_accused_bind_beside_it():
    # What the related-entity enricher leaves behind. That stage may never
    # propose `accused` (`enrich_related_entities.validate_new_bind`), so a
    # person it judged to be a defendant lands under `related`/`witness`/
    # `alleged` instead. When the court record then STATES they are a
    # defendant, this binder adds the authoritative `accused` bind --
    # `bind_key` is `(nes_id, relationship_type)`, so the pair is new.
    #
    # It does NOT retype or remove the wrong bind: `merge_entity_binds` never
    # overwrites, because the whole-list PATCH makes any omission destructive
    # and an existing bind can carry a human's notes. Both binds therefore
    # survive, and the stale one is a human's call, not this stage's.
    for role in RELATED_ROLES:
        existing = {"nes_id": YADAV, "type": role, "notes": "from the summary"}
        api = _PlanApi(detail={"registration_date_ad": "2023-06-22"},
                       parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव",
                                 "nes_id": YADAV}])
        plan = _plan(api, _case(entities=[existing]))
        assert [(i["nes_id"], i["relationship_type"]) for i in plan.entities] == [
            (YADAV, role), (YADAV, "accused")], f"role {role!r}"
        # The human's note on the pre-existing bind survives the merge.
        assert plan.entities[0]["notes"] == "from the summary"
        # And the accused bind is the only one carrying an outcome -- the
        # `outcome_only_on_accused` CHECK constraint rejects any other.
        assert "outcome" not in plan.entities[0]
        assert plan.entities[1]["outcome"] == CHARGED


def test_a_name_variant_of_an_already_bound_person_is_not_matched_to_them():
    # The failure mode behind the mis-typing question. If the related-entity
    # enricher already created an entity for this human under a DIFFERENT
    # spelling, rung 2's exact-name test cannot see it -- so the binder creates
    # a second person and the case carries two entities for one man, one
    # `related` and one `accused`. Asserted rather than fixed: only equality is
    # safe here, since कमला/कमल proves near-matches are different people.
    variant = build_entity_iri(PERSON_PREFIX, "krishna-prasad-yadab")
    api = _SearchApi([_hit(variant, "कृष्ण प्रसाद यादब")], complete=True)
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=["person"], run_entities={}, dry_run=True)
    assert got.how == "created"
    assert got.nes_id != variant


def test_an_existing_accused_bind_is_never_duplicated_or_reset():
    # The other half: when the wrong-typed bind is already the RIGHT type, the
    # pair matches and nothing is written at all -- so a re-run cannot reset a
    # caseworker's `convicted` verdict to this stage's `charged`. Whole-list
    # replace means "no change" has to mean sending no list.
    existing = {"nes_id": YADAV, "type": "accused", "outcome": "convicted",
                "notes": "hand-written by a caseworker"}
    api = _PlanApi(detail={"registration_date_ad": "2023-06-22"},
                   parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव",
                             "nes_id": YADAV}])
    plan = _plan(api, _case(entities=[existing]))
    assert plan.entities is None
