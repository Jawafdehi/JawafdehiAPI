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
    """`_PlanApi` plus the list/detail entry points `main()` calls before it
    ever reaches `plan_case` -- one case in, its own ETag on the read."""

    def __init__(self, case, **kw):
        super().__init__(**kw)
        self._case = case

    def iter_cases(self, params=None, timeout=60, progress=None):
        yield self._case

    def get_case_with_etag(self, slug, timeout=60):
        return self._case, 'W/"7"'

    def entity_prefixes(self, timeout=60):
        return ["person"]


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
    api = _CliApi(
        _case(),
        detail={"registration_date_ad": "2023-06-22"},
        hearings=[DECIDED],
        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV}],
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


def test_the_module_imports_without_django(tmp_path):
    """The standalone constraint, pinned. One convenience import re-adds Django."""
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    proc = subprocess.run(
        [sys.executable, "-c", "import casework.enrich_court_record"],
        env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
