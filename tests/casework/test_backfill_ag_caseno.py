# tests/casework/test_backfill_ag_caseno.py
import json
import os
import types
import urllib.error

import pytest

from casework.backfill_ag_caseno import (
    APPLIED, APPLY_FAILED, CONFLICT_QUARANTINE, GET_ERROR, GET_FATAL, GONE,
    GUARD_FAIL, GUARD_MISSING, SKIP_IDEMPOTENT, WOULD_APPLY, check_guards,
    merge_case_no, plan_one, run, select_targets, validate_map,
)

CASE_NO = "jawafdehi:caseNumber"


def doc(**over):
    """A stored AG material as the detail endpoint returns it."""
    base = {
        "@id": "https://jawafdehi.org/material/ag/83475",
        "name": {"ne": "जिल्ला कालिकोट"},
        "text": {"ne": "आरोप पत्र"},
        "associatedMedia": [{"@type": "MediaObject", "contentUrl": "x.pdf"}],
        "additionalType": "jawafdehi:ChargeSheet",
        "jawafdehi:recordId": "83475",
        "jawafdehi:officeLevel": 1,
        "jawafdehi:sourceType": "AG_ABHIYOG_PATRA",
    }
    base.update(over)
    return base


def row(**over):
    base = {"id": 83475, "material_iri": "https://jawafdehi.org/material/ag/83475",
            "case_number": "081-CR-0094", "source": "file", "ambiguous": "",
            "alt_case_number": "", "in_lake": "true"}
    base.update(over)
    return base


class FakeApi:
    def __init__(self, docs=None, error=None):
        self.docs = docs or {}
        self.error = error
        self.puts = []

    def get(self, path, timeout=60):
        if self.error:
            raise self.error
        ident = path.removeprefix("/materials/ag/").rstrip("/")
        if ident not in self.docs:
            raise urllib.error.HTTPError(path, 404, "nf", {}, None)
        return self.docs[ident]

    def put_material(self, source, ident, doc, material_type=None, timeout=60):
        self.puts.append((source, ident, doc, material_type))
        return {}


class FlakyApi:
    """Fails the first `fail_times` GETs with `code`, then serves `doc`.

    Models the production materials API, which 429s under a sustained walk.
    """

    def __init__(self, doc, fail_times, code):
        self._doc, self._fail_times, self._code = doc, fail_times, code
        self.attempts = 0

    def get(self, path, timeout=60):
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise urllib.error.HTTPError(path, self._code, "boom", {}, None)
        return self._doc


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def test_select_targets_excludes_absent_and_unrecoverable():
    rows = [row(), row(id=2, in_lake="false"), row(id=3, case_number="")]
    targets, skipped = select_targets(rows)
    assert [t["id"] for t in targets] == [83475]
    assert skipped == {"not_in_lake": 1, "unrecoverable": 1, "lake_state_unknown": 0}


def test_unknown_lake_state_is_not_reported_as_absent():
    # A --no-probe map leaves in_lake blank and a throttled probe leaves
    # "error". Counting either as not_in_lake would claim the lake was checked.
    _, skipped = select_targets([row(in_lake=""), row(id=2, in_lake="error")])
    assert skipped["lake_state_unknown"] == 2
    assert skipped["not_in_lake"] == 0


# --------------------------------------------------------------------------
# map validation -- the writer must not trust its input
# --------------------------------------------------------------------------

def test_validate_map_rejects_a_non_list_or_empty_map():
    for bad in ({}, [], {"a": row()}, [1], None):
        with pytest.raises(SystemExit):
            validate_map(bad, "map")


def test_validate_map_rejects_a_row_missing_a_dereferenced_key():
    # plan_one does row["material_iri"]; a KeyError mid-apply would leave the
    # backfill half-written with no resume marker.
    incomplete = {k: v for k, v in row().items() if k != "material_iri"}
    with pytest.raises(SystemExit, match="material_iri"):
        validate_map([incomplete], "map")


def test_validate_map_accepts_a_real_map():
    assert validate_map([row()], "map") == [row()]


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------

def test_guards_pass_on_matching_special_office_doc():
    outcome, guards = check_guards(doc(), 83475)
    assert outcome is None
    assert guards["officeLevel"] == 1


def test_guards_reject_record_id_mismatch():
    # Would otherwise stamp a case number onto a DIFFERENT document.
    outcome, _ = check_guards(doc(**{"jawafdehi:recordId": "999"}), 83475)
    assert outcome == GUARD_FAIL


def test_guards_reject_non_special_office_and_wrong_source_type():
    assert check_guards(doc(**{"jawafdehi:officeLevel": 3}), 83475)[0] == GUARD_FAIL
    assert check_guards(doc(**{"jawafdehi:sourceType": "OTHER"}), 83475)[0] == GUARD_FAIL


def test_absent_identity_fields_are_distinguished_from_a_mismatch():
    # `materials/sourcing/ag/shaper.py` emits NEITHER jawafdehi:recordId nor
    # jawafdehi:officeLevel, so a doc written by the in-repo shaper carries no
    # identity at all. That is "the stored shape changed", not "wrong record" --
    # conflating them would report an ingest-path change as 473 bad matches.
    shaped_by_the_repo = {k: v for k, v in doc().items()
                          if not k.startswith("jawafdehi:record")
                          and k != "jawafdehi:officeLevel"}
    assert check_guards(shaped_by_the_repo, 83475)[0] == GUARD_MISSING


def test_guards_reject_a_contradicting_additional_type():
    # The PUT sends material_type=charge_sheet explicitly; writing it onto a doc
    # whose own discriminator says otherwise would silently RETYPE the material.
    outcome, _ = check_guards(doc(additionalType="jawafdehi:PressRelease"), 83475)
    assert outcome == GUARD_FAIL


def test_a_missing_additional_type_is_tolerated():
    # Absence is exactly what the explicit material_type on the PUT covers --
    # and omitting it would let the server infer `document` from DigitalDocument.
    bare = {k: v for k, v in doc().items() if k != "additionalType"}
    assert check_guards(bare, 83475)[0] is None


# --------------------------------------------------------------------------
# merge -- the safety-critical part (PUT replaces `data` wholesale)
# --------------------------------------------------------------------------

def test_merge_preserves_every_existing_key():
    original = doc()
    merged, added = merge_case_no(original, row())
    for key in original:
        assert merged[key] == original[key], f"{key} was mutated or dropped"
    assert merged[CASE_NO] == "081-CR-0094"
    assert added == [CASE_NO, "jawafdehi:caseNumberSource"]


def test_merge_does_not_mutate_the_input_doc():
    original = doc()
    merge_case_no(original, row())
    assert CASE_NO not in original


def test_merge_flags_ambiguous_and_keeps_alternate():
    merged, added = merge_case_no(
        doc(), row(ambiguous="true", alt_case_number="081-CR-0009"))
    assert merged["jawafdehi:caseNumberAmbiguous"] is True
    # A LIST -- the map's `;` join is a CSV artefact, not a JSON-LD value.
    assert merged["jawafdehi:caseNumberAlt"] == ["081-CR-0009"]
    assert len(added) == 4


def test_multiple_alternates_are_a_list_not_a_delimited_string():
    merged, _ = merge_case_no(
        doc(), row(ambiguous="true", alt_case_number="081-CR-0009;082-CR-0011"))
    assert merged["jawafdehi:caseNumberAlt"] == ["081-CR-0009", "082-CR-0011"]


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def test_plan_would_apply_when_field_absent():
    plan = plan_one(FakeApi({"83475": doc()}), row())
    assert plan["outcome"] == WOULD_APPLY
    assert plan["merged"][CASE_NO] == "081-CR-0094"


def test_plan_is_idempotent_when_value_already_correct():
    api = FakeApi({"83475": doc(**{CASE_NO: "081-CR-0094"})})
    assert plan_one(api, row())["outcome"] == SKIP_IDEMPOTENT


def test_plan_quarantines_a_different_existing_value():
    api = FakeApi({"83475": doc(**{CASE_NO: "079-CR-0001"})})
    plan = plan_one(api, row())
    assert plan["outcome"] == CONFLICT_QUARANTINE
    assert plan["merged"] is None  # nothing to write


def test_plan_guard_fail_produces_no_merge():
    api = FakeApi({"83475": doc(**{"jawafdehi:officeLevel": 3})})
    plan = plan_one(api, row())
    assert plan["outcome"] == GUARD_FAIL
    assert plan["merged"] is None


def test_plan_reports_gone_and_transport_error_distinctly():
    # A 404 is definitive and must NOT be retried; a transport error is
    # retryable and only becomes GET_ERROR once the retries are spent.
    assert plan_one(FakeApi({}), row(), retries=0)["outcome"] == GONE
    api = FakeApi({"83475": doc()}, error=urllib.error.URLError("boom"))
    assert plan_one(api, row(), retries=0, interval=0)["outcome"] == GET_ERROR


def test_throttled_get_is_retried_then_succeeds():
    # 429 twice, then the real document -- must end in a plan, not GET_ERROR.
    api = FlakyApi(doc(), fail_times=2, code=429)
    plan = plan_one(api, row(), retries=3, interval=0)
    assert plan["outcome"] == WOULD_APPLY
    assert api.attempts == 3


def test_404_is_not_retried():
    api = FlakyApi(doc(), fail_times=99, code=404)
    assert plan_one(api, row(), retries=3, interval=0)["outcome"] == GONE
    assert api.attempts == 1  # definitive: one shot only


@pytest.mark.parametrize("code", [400, 401, 403, 410])
def test_client_errors_are_not_retried(code):
    # An expired token (401) or a missing NGM role (403) cannot improve by
    # waiting. Retrying would sleep the whole ladder on EVERY row -- ~15s x 473
    # rows -- before reporting what the first response already said.
    api = FlakyApi(doc(), fail_times=99, code=code)
    outcome = plan_one(api, row(), retries=4, interval=0)["outcome"]
    assert outcome in (GET_FATAL, GONE)
    assert api.attempts == 1


def test_5xx_is_still_retried():
    api = FlakyApi(doc(), fail_times=2, code=503)
    assert plan_one(api, row(), retries=3, interval=0)["outcome"] == WOULD_APPLY
    assert api.attempts == 3


# --------------------------------------------------------------------------
# run() -- dry-run must never write
# --------------------------------------------------------------------------

def _args(map_path, **over):
    base = dict(map=str(map_path), dry_run=True, limit=0, verbose=False,
                put_bodies="", write_interval=0, api_token="tok",
                api_base_url="http://127.0.0.1:48010", allow_remote_writes=False,
                # no real backoff in tests
                read_retries=0, read_interval=0, max_consecutive_failures=10,
                lock_file="")
    base.update(over)
    return types.SimpleNamespace(**base)


def _write_map(tmp_path, rows):
    p = tmp_path / "map.json"
    p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return p


def test_dry_run_plans_but_sends_no_put(tmp_path, monkeypatch):
    api = FakeApi({"83475": doc()})
    monkeypatch.setattr("casework.backfill_ag_caseno._build_api", lambda a: api)
    stats = run(_args(_write_map(tmp_path, [row()])))
    assert stats[WOULD_APPLY] == 1
    assert api.puts == []          # the whole point


def test_apply_sends_the_complete_merged_document(tmp_path, monkeypatch):
    api = FakeApi({"83475": doc()})
    monkeypatch.setattr("casework.backfill_ag_caseno._build_api", lambda a: api)
    stats = run(_args(_write_map(tmp_path, [row()]), dry_run=False))
    assert stats[APPLIED] == 1
    _, ident, sent, mtype = api.puts[0]
    assert (ident, mtype) == ("83475", "charge_sheet")
    # a partial PUT would destroy these -- assert the whole doc went back
    assert sent["text"] == {"ne": "आरोप पत्र"}
    assert sent["associatedMedia"]
    assert sent[CASE_NO] == "081-CR-0094"


def test_put_bodies_are_recorded_for_inspection(tmp_path, monkeypatch):
    api = FakeApi({"83475": doc()})
    monkeypatch.setattr("casework.backfill_ag_caseno._build_api", lambda a: api)
    bodies = tmp_path / "bodies.jsonl"
    run(_args(_write_map(tmp_path, [row()]), put_bodies=str(bodies)))
    written = json.loads(bodies.read_text(encoding="utf-8").strip())
    assert written["method"] == "PUT"
    assert written["body"]["material"][CASE_NO] == "081-CR-0094"


def test_run_counts_skipped_rows_without_fetching_them(tmp_path, monkeypatch):
    api = FakeApi({"83475": doc()})
    monkeypatch.setattr("casework.backfill_ag_caseno._build_api", lambda a: api)
    rows = [row(), row(id=2, in_lake="false"), row(id=3, case_number="")]
    stats = run(_args(_write_map(tmp_path, rows)))
    assert stats["skipped_not_in_lake"] == 1
    assert stats["skipped_unrecoverable"] == 1


# --------------------------------------------------------------------------
# circuit breaker -- a systemic fault must not walk the whole map
# --------------------------------------------------------------------------

class DeadApi:
    """Every GET 500s -- an API that is down, or the wrong host entirely."""

    def __init__(self):
        self.calls = 0

    def get(self, path, timeout=60):
        self.calls += 1
        raise urllib.error.HTTPError(path, 500, "boom", {}, None)


def test_a_streak_of_failures_aborts_instead_of_walking_the_whole_map(
        tmp_path, monkeypatch):
    api = DeadApi()
    monkeypatch.setattr("casework.backfill_ag_caseno._build_api", lambda a: api)
    rows = [row(id=i) for i in range(1, 51)]
    stats = run(_args(_write_map(tmp_path, rows), read_retries=0,
                      max_consecutive_failures=3))
    assert stats["ABORTED_CONSECUTIVE_FAILURES"] == 3
    assert api.calls == 3            # stopped at 3, not 50
    assert stats[GET_ERROR] == 3


def test_the_breaker_resets_on_a_success_so_isolated_failures_pass_through(
        tmp_path, monkeypatch):
    # 404, ok, 404 -- three rows, never 2 failures in a row.
    api = FakeApi({"2": doc(**{"@id": "https://jawafdehi.org/material/ag/2",
                               "jawafdehi:recordId": "2"})})
    monkeypatch.setattr("casework.backfill_ag_caseno._build_api", lambda a: api)
    rows = [row(id=1), row(id=2, material_iri="https://jawafdehi.org/material/ag/2"),
            row(id=3)]
    stats = run(_args(_write_map(tmp_path, rows), max_consecutive_failures=2))
    assert "ABORTED_CONSECUTIVE_FAILURES" not in stats
    assert stats[GONE] == 2 and stats[WOULD_APPLY] == 1


def test_a_systemic_apply_failure_aborts_the_write_pass(tmp_path, monkeypatch):
    class RefusingApi(FakeApi):
        def put_material(self, *a, **kw):
            raise urllib.error.HTTPError("/m", 403, "no ngm role", {}, None)

    api = RefusingApi({str(i): doc(**{
        "@id": f"https://jawafdehi.org/material/ag/{i}",
        "jawafdehi:recordId": str(i)}) for i in range(1, 21)})
    monkeypatch.setattr("casework.backfill_ag_caseno._build_api", lambda a: api)
    rows = [row(id=i, material_iri=f"https://jawafdehi.org/material/ag/{i}")
            for i in range(1, 21)]
    stats = run(_args(_write_map(tmp_path, rows), dry_run=False,
                      max_consecutive_failures=3))
    assert stats["ABORTED_CONSECUTIVE_FAILURES"] == 3
    assert stats[APPLY_FAILED] == 3  # not 20 doomed writes


# --------------------------------------------------------------------------
# single-instance lock -- this verb must not race itself
#
# The materials endpoint has no ETag/If-Match, so two overlapping
# GET->merge->PUT passes silently lose each other's writes. This already bit
# once: three stray concurrent processes tripled the request rate during the
# recovery dry-run and produced a spray of 429s that read as lake errors.
# --------------------------------------------------------------------------

def test_a_second_instance_refuses_to_start(tmp_path):
    from casework.backfill_ag_caseno import single_instance
    lock = tmp_path / "backfill.lock"
    with single_instance(lock):
        assert lock.exists()
        with pytest.raises(SystemExit, match="holds"):
            with single_instance(lock):
                raise AssertionError("the second instance must not get in")


def test_the_lock_is_released_even_when_the_run_raises(tmp_path):
    from casework.backfill_ag_caseno import single_instance
    lock = tmp_path / "backfill.lock"
    with pytest.raises(ValueError):
        with single_instance(lock):
            raise ValueError("boom")
    assert not lock.exists()          # a crashed run must not wedge the next one
    with single_instance(lock):       # and the next one starts cleanly
        assert lock.exists()


def test_a_stale_lock_from_a_dead_process_is_reclaimed(tmp_path):
    from casework.backfill_ag_caseno import single_instance
    lock = tmp_path / "backfill.lock"
    # PID 2^22 is above Linux's default pid_max: nothing owns it.
    lock.write_text("4194304", encoding="utf-8")
    with single_instance(lock):
        assert lock.read_text(encoding="utf-8") == str(os.getpid())


def test_an_unparseable_lock_is_treated_as_stale(tmp_path):
    # A truncated/garbage lock file must not wedge the verb forever.
    from casework.backfill_ag_caseno import single_instance
    lock = tmp_path / "backfill.lock"
    lock.write_text("not-a-pid", encoding="utf-8")
    with single_instance(lock):
        assert lock.read_text(encoding="utf-8") == str(os.getpid())


def test_run_takes_the_lock_and_releases_it(tmp_path, monkeypatch):
    api = FakeApi({"83475": doc()})
    monkeypatch.setattr("casework.backfill_ag_caseno._build_api", lambda a: api)
    lock = tmp_path / "run.lock"
    stats = run(_args(_write_map(tmp_path, [row()]), lock_file=str(lock)))
    assert stats[WOULD_APPLY] == 1
    assert not lock.exists()
