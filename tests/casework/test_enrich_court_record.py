"""The court-record binder: defendant resolution, and the patch it plans.

The date and stage half lives in `test_case_stages.py` -- this file covers the
binder, the same-case skip, and the plan the two produce together.
"""

#: A court record as `court_record_for_case` now returns it -- with `iri`, the
#: reference's own IRI, which the stage that cites it must carry verbatim.
def _record(reg=None, hearings=(), status=None, parties=(), number="079-cr-0151"):
    return {"court": "special", "number": number,
            "iri": f"https://jawafdehi.org/courtcase/special/{number}",
            "detail": {"registration_date_ad": reg, "case_status": status},
            "hearings": list(hearings), "parties": list(parties)}


DECIDED = {"case_status": "फैसला", "decision_type": "सफाई",
           "hearing_date_ad": "2024-06-04", "hearing_date_bs": "2081-02-22"}
ADJOURNED = {"case_status": "स्थगित", "decision_type": "पक्षबाट",
             "hearing_date_ad": "2024-05-27", "hearing_date_bs": "2081-02-14"}


import urllib.error  # noqa: E402

from casework.common.api import EntityAlreadyExists  # noqa: E402
from casework.case_stages import reference_end, trial_stage  # noqa: E402
from casework.entity_identity import MAX_SLUG_LENGTH, entity_slug  # noqa: E402
from casework.entity_resolver import normalise_name  # noqa: E402
from casework.enrich_court_record import (  # noqa: E402
    MAX_SLUG_SUFFIX,
    PERSON_PREFIX,
    _accused_binds,
    _is_person,
    already_bound,
    bound_accused_keys,
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


class _SlugAwareApi(_SearchApi):
    """Refuses a POST whose slug is taken, exactly as a 409 does in prod."""

    def __init__(self, results=(), taken=()):
        super().__init__(results)
        self.taken = set(taken)

    def create_entity(self, payload, timeout=60):
        slug = payload["slug"]
        if slug in self.taken:
            raise EntityAlreadyExists(f"{slug} exists")
        self.taken.add(slug)
        self.posted.append(payload)
        return {"@id": build_entity_iri(PERSON_PREFIX, slug)}


def test_a_row_carrying_an_nes_id_is_still_a_pure_copy():
    api = _SlugAwareApi()
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", YADAV, citation="",
                            live_prefixes=["person"], dry_run=False)
    assert (got.nes_id, got.how) == (YADAV, "nes_id")
    assert api.posted == []


def test_a_row_nes_id_that_is_not_a_person_is_still_refused():
    api = _SlugAwareApi()
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", ORG, citation="",
                            live_prefixes=["person"], dry_run=False)
    assert (got.nes_id, got.how) == ("", "failed")
    assert "not a person entity" in got.reason


def test_an_existing_namesake_in_nes_is_ignored_and_a_new_entity_is_made():
    # The whole point of the rebuild: no search, no reuse. NES holds 13 rows
    # for संजय प्रसाद यादव; none of them may be assumed to be this defendant.
    api = _SlugAwareApi(results=[_hit(YADAV, "कृष्ण प्रसाद यादव")])
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=["person"], dry_run=False)
    assert got.how == "created"
    assert len(api.posted) == 1


def test_the_search_endpoint_is_never_called_at_all():
    class _NoSearch(_SlugAwareApi):
        def search_entities(self, query, **kwargs):
            raise AssertionError("the rebuilt resolver must never search NES")

    got = resolve_defendant(_NoSearch(), "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=["person"], dry_run=False)
    assert got.how == "created"


def test_a_taken_slug_is_suffixed_and_created_rather_than_refused():
    base = entity_slug("कृष्ण प्रसाद यादव")
    api = _SlugAwareApi(taken={base})
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=["person"], dry_run=False)
    assert got.nes_id.endswith(f"/{base}-2")
    assert got.how == "created"


def test_the_suffix_climbs_until_a_slug_is_free():
    base = entity_slug("कृष्ण प्रसाद यादव")
    api = _SlugAwareApi(taken={base, f"{base}-2", f"{base}-3"})
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=["person"], dry_run=False)
    assert got.nes_id.endswith(f"/{base}-4")


def test_the_suffix_gives_up_at_the_cap_and_reports_rather_than_raising():
    base = entity_slug("कृष्ण प्रसाद यादव")
    api = _SlugAwareApi(
        taken={base} | {f"{base}-{n}" for n in range(2, MAX_SLUG_SUFFIX + 5)})
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=["person"], dry_run=False)
    assert (got.nes_id, got.how) == ("", "failed")
    assert str(MAX_SLUG_SUFFIX) in got.reason


def test_two_identical_names_on_one_case_become_two_entities():
    api = _SlugAwareApi()
    first = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                              live_prefixes=["person"], dry_run=False)
    second = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                               live_prefixes=["person"], dry_run=False)
    assert first.nes_id != second.nes_id
    assert second.nes_id.endswith("-2")


def test_the_same_name_on_two_cases_becomes_two_entities():
    # Cross-case reuse is deliberately gone: two cases naming one person get
    # two entities, and a human merges them.
    api = _SlugAwareApi()
    a = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                          live_prefixes=["person"], dry_run=False)
    b = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                          live_prefixes=["person"], dry_run=False)
    assert a.nes_id != b.nes_id


def test_a_suffix_stays_within_the_slug_length_bound():
    long_name = "कृष्ण प्रसाद " * 12
    base = entity_slug(long_name)
    api = _SlugAwareApi(taken={base})
    got = resolve_defendant(api, long_name, None, citation="",
                            live_prefixes=["person"], dry_run=False)
    slug = got.nes_id.rsplit("/", 1)[-1]
    assert len(slug) <= MAX_SLUG_LENGTH
    assert slug.endswith("-2")


def test_the_citation_rides_on_the_created_entity():
    api = _SlugAwareApi()
    resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="mat-1",
                      live_prefixes=["person"], dry_run=False)
    assert api.posted[0]["citation"] == "mat-1"


def test_a_dry_run_posts_nothing_and_admits_the_slug_may_shift():
    api = _SlugAwareApi()
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=["person"], dry_run=True)
    assert api.posted == []
    assert got.how == "created"
    assert "would create" in got.reason and "-N suffix" in got.reason


def test_an_unreadable_prefix_list_is_not_a_verdict_on_the_prefix():
    # `read_live_prefixes` returns None on any error (a transient 502 at run
    # start), and `prefix_is_creatable` folds None to the empty set -- so
    # without a dedicated branch every defendant needing creation is reported
    # "the person prefix is not creatable", a false statement about a prefix
    # as ordinary as `person`.
    api = _SlugAwareApi()
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=None, dry_run=True)
    assert (got.nes_id, got.how) == ("", "failed")
    assert "could not be read" in got.reason and "retry this case" in got.reason
    assert "not creatable" not in got.reason
    assert api.posted == []


def test_a_genuinely_unusable_prefix_still_says_so():
    api = _SlugAwareApi()
    got = resolve_defendant(api, "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=[], dry_run=True)
    assert (got.nes_id, got.how) == ("", "failed")
    assert "not creatable" in got.reason


def test_a_name_that_cannot_be_slugged_fails_without_raising():
    api = _SlugAwareApi()
    got = resolve_defendant(api, "   ", None, citation="",
                            live_prefixes=["person"], dry_run=True)
    assert (got.nes_id, got.how) == ("", "failed")
    assert "slug" in got.reason


def test_a_failed_post_costs_this_name_and_not_the_run():
    class _FlakyApi(_SlugAwareApi):
        def create_entity(self, payload, timeout=60):
            raise urllib.error.HTTPError("https://jawafdehi.org", 502,
                                         "Bad Gateway", {}, None)

    got = resolve_defendant(_FlakyApi(), "कृष्ण प्रसाद यादव", None, citation="",
                            live_prefixes=["person"], dry_run=False)
    assert got.how == "failed"
    assert "HTTPError" in got.reason


# ── the same-case skip ───────────────────────────────────────────────────────

def _bound(nes_id, name, rel="accused"):
    return {"nes_id": nes_id, "display_name": name, "type": rel}


#: Derived, never hand-spelled: `entity_slug` keeps the schwa
#: (`krishna-prasada-yadava`), and a hardcoded guess made the slug half of the
#: skip rule silently untested.
BOUND_SLUG = entity_slug("कृष्ण प्रसाद यादव")
RAMA = build_entity_iri(PERSON_PREFIX, BOUND_SLUG)


def test_bound_accused_keys_holds_the_name_and_the_slug():
    keys = bound_accused_keys({"entities": [_bound(RAMA, "कृष्ण प्रसाद यादव")]})
    assert normalise_name("कृष्ण प्रसाद यादव") in keys
    assert BOUND_SLUG in keys


def test_a_name_already_bound_on_this_case_is_skipped():
    keys = bound_accused_keys({"entities": [_bound(RAMA, "कृष्ण प्रसाद यादव")]})
    assert already_bound("कृष्ण प्रसाद यादव", keys) is True


def test_a_different_name_on_the_case_is_not_skipped():
    keys = bound_accused_keys({"entities": [_bound(RAMA, "कृष्ण प्रसाद यादव")]})
    assert already_bound("सीता देवी", keys) is False


def test_the_slug_matches_even_when_nes_cannot_resolve_the_name():
    # `display_name` is None when the NES resolver cannot reach the entity.
    keys = bound_accused_keys({"entities": [_bound(RAMA, None)]})
    assert already_bound("कृष्ण प्रसाद यादव", keys) is True


def test_a_suffixed_slug_still_matches_the_bare_name():
    keys = bound_accused_keys({"entities": [_bound(RAMA + "-2", "कृष्ण")]})
    assert already_bound("कृष्ण प्रसाद यादव", keys) is True


def test_a_non_accused_bind_never_blocks_a_defendant():
    keys = bound_accused_keys(
        {"entities": [_bound(RAMA, "कृष्ण प्रसाद यादव", rel="related")]})
    assert already_bound("कृष्ण प्रसाद यादव", keys) is False


def test_a_case_with_no_binds_blocks_nothing():
    assert bound_accused_keys({"entities": []}) == set()
    assert bound_accused_keys({}) == set()


def test_a_malformed_bind_iri_contributes_no_key_and_does_not_raise():
    keys = bound_accused_keys({"entities": [_bound("not-an-iri", "कृष्ण")]})
    assert already_bound("कृष्ण प्रसाद यादव", keys) is False


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
            "dates": {"stages": []}, "entities": []}
    base.update(over)
    return base


def _plan(api, case, **kw):
    kw.setdefault("live_prefixes", ["person"])
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
    # The retired columns are gone: the write is the whole stage document.
    assert [f for f, _ in plan.fields] == ["dates"]
    assert plan.stages == {"stages": [
        {"stage": "initial", "start": "2023-06-22", "end": "2024-06-04",
         "courtcase_iri": CASE_IRI}]}
    assert plan.entities == [{"nes_id": YADAV, "relationship_type": "accused",
                              "outcome": ACQUITTED,
                              "notes": "प्रतिवादी — विशेष अदालत मुद्दा 079-cr-0151"}]
    assert plan.status == "would-patch"


def test_a_plaintiff_is_never_bound():
    api = _PlanApi(detail={"registration_date_ad": "2023-06-22"},
                   parties=[{"side": "plaintiff", "name": "नेपाल सरकार"}])
    assert _plan(api, _case()).entities is None


def test_the_new_entity_citation_comes_from_the_trial_record():
    # A writ (`OA`) material never names these defendants, so citing it would
    # put a false provenance claim on a public NES record. `trial_refs` now
    # keeps the OA reference out of `records` entirely, so the citation can
    # only ever come from a trial docket.
    cr_record = _record(number="079-cr-0151",
                        parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}])
    cr_record["detail"]["material_id"] = "https://jawafdehi.org/material/court/special.079-cr-0151"
    api = _SlugAwareApi()
    items, rows, skips = _accused_binds(
        api, _case(), [cr_record], live_prefixes=["person"], dry_run=False)
    assert len(items) == 1
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
        live_prefixes=["person"], dry_run=True)
    assert [i["nes_id"] for i in items] == [YADAV]
    assert skips == []


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
        live_prefixes=["person"], dry_run=True)
    assert [i["nes_id"] for i in items] == [YADAV]
    assert skips == []


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
        live_prefixes=["person"], dry_run=True)
    assert len(rows) == 1
    assert [i["nes_id"] for i in items] == [YADAV]


def test_a_case_with_only_a_non_prosecution_reference_gets_no_stage():
    # The REVERSE of the old rule, and deliberately so. Under one date span a
    # writ's dates were better than nothing; under stages they would assert
    # that a writ is this case's first instance. The reference is reported for
    # the appeal enricher and nothing is written.
    api = _PlanApi(detail={"registration_date_ad": "2023-06-22"}, hearings=[DECIDED],
                   parties=[{"side": "defendant", "name": "कुनै व्यक्ति"}])
    case = _case(court_cases=["https://jawafdehi.org/courtcase/special/079-oa-0014"])
    plan = _plan(api, case)
    assert plan.stages is None
    assert plan.fields == []
    assert plan.entities is None
    assert any("079-oa-0014" in s and "appeal enricher" in s for s in plan.skips)
    assert any("no trial docket" in s for s in plan.skips)


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


def test_a_case_with_nothing_to_change_is_a_skip():
    api = _PlanApi(detail={}, parties=[])
    plan = _plan(api, _case())
    assert plan.status == "nothing-to-do"
    assert plan.fields == [] and plan.entities is None and plan.stages is None


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

import importlib  # noqa: E402

from casework.enrich_court_record import (  # noqa: E402
    apply_plan,
    main,
    stage_table,
)


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
    # A non-trial docket is reported under `step="bind_plan"` (see
    # `_NON_TRIAL_SKIP_MARKER`), never into the `dates`/`no_source` catch-all a
    # genuine date-derivation skip uses. The case carries a TRIAL docket too,
    # so it gets past `select` and reaches the bind plan at all -- a case whose
    # only docket is non-trial is terminal at `select` instead.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    oa_ref = "https://jawafdehi.org/courtcase/special/079-oa-0014"
    case = _case(court_cases=[CASE_IRI, oa_ref])
    api = _CliApi(case, detail={"registration_date_ad": "2023-06-22"},
                  hearings=[DECIDED],
                  parties=[{"side": "defendant", "name": "कुनै व्यक्ति"}])
    import casework.enrich_court_record as ecr
    monkeypatch.setattr(ecr, "build_api", lambda args: api)

    rc = main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run",
               "--review-file", str(tmp_path / "review.md")])
    assert rc == 0
    events = _events(tmp_path)

    bind_plan = [e for e in events if e["step"] == "bind_plan"]
    assert any("079-oa-0014" in e["detail"] and "appeal enricher" in e["detail"]
               for e in bind_plan)
    assert any("left for the appeal enricher" in e["detail"] for e in bind_plan)
    # The skip must not ALSO (or instead) land under `dates`.
    dates = [e for e in events if e["step"] == "dates"]
    assert not any("079-oa-0014" in e.get("detail", "") for e in dates)
    assert {e["status"] for e in bind_plan} == {"ok"}


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
    # A dry run makes no POST, so it cannot know whether the base slug is
    # free. The caveat has to say that rather than imply the printed IRI is
    # the one an --apply would use.
    assert "-N suffix" in created[0]["detail"]
    assert api.posted == []


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
    assert "### Stages and defendants" in text
    assert "कृष्ण प्रसाद यादव" in text
    assert "सिताराम यादव" in text
    # DECIDED is a plain सफाई on the only reference, so the case acquits -- and
    # the summary line has to say so, not just count the binds.
    assert f"accused+2 ({ACQUITTED})" in text
    assert ACQUITTED in text.split("### Stages and defendants")[1]


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
    api = _CliApi(_case(dates={"stages": [
                      {"stage": "initial", "start": "2023-06-22",
                       "end": "2024-06-04", "courtcase_iri": CASE_IRI}]}),
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


# ------------------------------------------------------------ different: split

# ------------------------------------------------------------- same: one entity

# ------------------------------------------------------------------ provenance

# ------------------------------------------------------------------ CLI wiring

SHARED = "कृष्ण प्रसाद यादव"


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


# --------------------------------------------------- the `भन्ने` alias, end to end

ALIAS_RAW = "आवास भन्ने आभाश अर्याल"
ALIAS_LEGAL = "आभाश अर्याल"


def test_the_bind_uses_the_legal_name_not_the_raw_court_string():
    # Run b2f31c03 created `person/avasa-bhanne-abhasha-aryala` for this row --
    # a name the man holds nowhere. The entity must be built from the legal name.
    api = _SearchApi(results=[], created={"@id": YADAV})
    items, rows, _ = _accused_binds(
        api, _case(), [_record(parties=[{"side": "defendant", "name": ALIAS_RAW}])],
        live_prefixes=["person"], dry_run=False)
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
        live_prefixes=["person"], dry_run=False)
    assert ALIAS_RAW in items[0]["notes"]
    assert "प्रतिवादी" in items[0]["notes"]


def test_the_dry_run_slug_no_longer_carries_the_alias_marker():
    got = resolve_defendant(_SlugAwareApi(), ALIAS_LEGAL, None, citation="",
                            live_prefixes=["person"], dry_run=True)
    assert "bhanne" not in got.nes_id
    assert got.nes_id.endswith("abhasha-aryala")


def test_the_accused_table_shows_the_alias_beside_the_legal_name():
    table = accused_table([_row(name=ALIAS_LEGAL, aliases=["आवास"])])
    assert ALIAS_LEGAL in table
    assert "भन्ने: आवास" in table


# --------------------------------------------------------------- run-level tally

def test_the_rung_tally_prints_every_rung_including_its_zeroes():
    # `0 already bound` is the number worth reading now: on a re-run over the
    # 1,140 cases that already carry binds it should dominate, and a tally
    # that dropped zeroes could not show it going to zero on a fresh case.
    rows = [_row(how="created"), _row(how="created"), _row(how="nes_id")]
    assert rung_summary(rows) == ("3 defendant(s): 1 copied, 2 created, "
                                  "0 already bound, 0 failed")


def test_the_rung_tally_of_no_defendants_still_reads():
    assert rung_summary([]) == ("0 defendant(s): 0 copied, 0 created, "
                                "0 already bound, 0 failed")


def test_the_rung_tally_counts_skipped_and_failed_rows_separately():
    rows = [_row(how="skipped", nes_id=""), _row(how="failed", nes_id=""),
            _row(how="nes_id")]
    assert rung_summary(rows) == ("3 defendant(s): 1 copied, 0 created, "
                                  "1 already bound, 1 failed")


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
    table = accused_table([_row(), _row(name="सीता देवी पौडेल", how="skipped")])
    assert "कृष्ण प्रसाद यादव" in table
    assert "सीता देवी पौडेल" in table
    assert table.count(CHARGED) == 2
    assert "created" in table and "skipped_already_bound" in table


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
    # `nes_id`, one is already bound on this case and is skipped, and one is
    # created -- all on one case. An implementation that picked a rung per case
    # would flatten these, and the 27th defendant on a case would inherit the
    # 1st one's fate.
    already = {"nes_id": build_entity_iri(PERSON_PREFIX,
                                          entity_slug("सीता देवी पौडेल")),
               "display_name": "सीता देवी पौडेल", "type": "accused"}
    parties = [{"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV},
               {"side": "defendant", "name": "सीता देवी पौडेल"},
               {"side": "defendant", "name": "हरि प्रसाद शर्मा"}]
    api = _SlugAwareApi()
    items, rows, _ = _accused_binds(
        api, _case(entities=[already]), [_record(parties=parties)],
        live_prefixes=["person"], dry_run=True)
    assert [r["how"] for r in rows] == ["nes_id", "skipped", "created"]
    # The skipped row contributes NO bind item -- it is already bound.
    assert items[0]["nes_id"] == YADAV
    assert len(items) == 2


def test_one_defendant_named_on_two_references_of_one_case_binds_once():
    # De-duplication is by normalised NAME across every reference on the case.
    # Without it a person named on both references of a two-reference case
    # would get two identical binds in the same PATCH body.
    party = {"side": "defendant", "name": "कृष्ण प्रसाद यादव", "nes_id": YADAV}
    records = [_record(number="079-cr-0151", parties=[party]),
               _record(number="080-cr-0002", parties=[dict(party)])]
    items, rows, _ = _accused_binds(
        _SearchApi(), _case(), records,
        live_prefixes=["person"], dry_run=True)
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
    assert reference_end(as_decision) == "2024-06-04"

    as_status = _record(reg="2023-06-22", status="तामेली")
    assert bind_outcome([as_status]) == CHARGED
    # No verdict date anywhere, so the stage stays OPEN rather than carrying a
    # guessed end.
    assert reference_end(as_status) == ""
    assert "end" not in trial_stage(as_status)


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


# ── the stage this enricher writes, end to end ───────────────────────────────

def test_the_enricher_makes_no_model_call_at_all():
    # The hold was this stage's only model call. With it gone the module must
    # not reach the LLM stack even indirectly, or a corpus run silently starts
    # spending tokens against a pooled subscription.
    from pathlib import Path

    import casework.enrich_court_record as ecr
    source = Path(ecr.__file__).read_text(encoding="utf-8")
    for forbidden in ("held_identity", "bootstrap", "invoke_json", "tier_for",
                      "UsageAccumulator"):
        assert forbidden not in source, forbidden


def test_the_held_compare_flag_is_gone():
    with pytest.raises(SystemExit):
        main(["--dry-run", "--no-held-compare"])


def test_a_run_reads_each_case_exactly_once():
    # The two-pass split existed only to build the cross-case name index.
    api = _CliApi(_case(), detail={"registration_date_ad": "2023-06-22"},
                  hearings=[DECIDED], parties=[])
    import casework.enrich_court_record as ecr
    ecr.build_api = lambda args: api
    try:
        main(["--api-base-url", "http://127.0.0.1:48010", "--dry-run"])
    finally:
        importlib.reload(ecr)
    assert api.call_counts["get_case_with_etag"] == 1
    assert api.call_counts["get_courtcase"] == 1


def test_the_review_file_shows_each_stage_and_its_docket():
    plan = CasePlan("c", "would-patch",
                    stages={"stages": [{"stage": "initial", "start": "2022-08-01",
                                        "end": "2024-06-04",
                                        "courtcase_iri": CASE_IRI}]})
    table = stage_table(plan)
    assert "initial" in table
    assert "2022-08-01" in table and "2024-06-04" in table
    assert "special/079-cr-0151" in table


def test_an_open_stage_reads_open_rather_than_blank():
    plan = CasePlan("c", "would-patch",
                    stages={"stages": [{"stage": "initial", "start": "2022-08-01",
                                        "courtcase_iri": CASE_IRI}]})
    assert "open" in stage_table(plan)


def test_a_plan_with_no_stage_change_renders_no_table():
    assert stage_table(CasePlan("c", "nothing-to-do")) == ""


def test_a_docket_carrying_no_date_at_all_produces_no_stage():
    # Migration 0068's rule, kept: a date makes a stage, a docket does not. A
    # dateless `initial` record reads as an OPEN court proceeding, so writing
    # one would flip a dateless draft's derived status to ONGOING on no
    # evidence.
    api = _PlanApi(detail={}, hearings=[],
                   parties=[{"side": "defendant", "name": "कृष्ण प्रसाद यादव"}])
    plan = _plan(api, _case())
    assert plan.stages is None
    assert plan.entities is not None      # the defendant is still bound
