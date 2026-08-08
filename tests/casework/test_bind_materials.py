# tests/casework/test_bind_materials.py
import glob
import json
import os
import types
import urllib.error

import pytest

from casework.common.evidence import merge_evidence
from casework.bind_materials import (
    BindPlan, _build_api, _ledger_status, apply_plan, candidates_from_row,
    missing_candidates, parse_source_ident, plan_case, run,
)

HOST = "https://jawafdehi.org/material"
PR = f"{HOST}/ciaa_press_release/2037"
CO = f"{HOST}/court_order/special.078-cr-0042"


# ---------------------------------------------------------------------------
# A fake CaseworkApi: scripts material existence + records replace_list writes.
# ---------------------------------------------------------------------------


class FakeApi:
    def __init__(self, exists=(), absent=(), uncertain=(), cases=None):
        self.exists = set(exists)        # "source/ident" strings that 200
        self.absent = set(absent)        # -> HTTP 404
        self.uncertain = set(uncertain)  # -> transport error
        self.cases = cases or {}         # slug -> (case_dict, etag)
        self.replaced = []               # recorded replace_list calls

    def get(self, path, timeout=60):
        key = path.removeprefix("/materials/").rstrip("/")
        if key in self.uncertain:
            raise urllib.error.URLError("boom")
        if key in self.exists:
            return {"@id": key}
        # absent set OR anything not declared present -> 404
        raise urllib.error.HTTPError(path, 404, "nf", {}, None)

    def get_case_with_etag(self, slug, timeout=60):
        return self.cases[slug]

    def replace_list(self, slug, path, items, if_match=None, timeout=60):
        self.replaced.append((slug, path, items, if_match))
        return {"ok": True}


# ---------------------------------------------------------------------------
# parse_source_ident / candidates_from_row
# ---------------------------------------------------------------------------


def test_parse_source_ident_extracts_source_and_ident():
    assert parse_source_ident(PR) == ("ciaa_press_release", "2037")


def test_parse_source_ident_lowercases_court_order():
    # uppercase ident 400s server-side; parsing must normalize it.
    up = f"{HOST}/court_order/special.078-CR-0042"
    assert parse_source_ident(up) == ("court_order", "special.078-cr-0042")


@pytest.mark.parametrize("bad", ["", "not-an-iri", "https://x/material/ag", None])
def test_parse_source_ident_rejects_garbage(bad):
    assert parse_source_ident(bad) is None


def test_candidates_from_row_strips_status_dedupes_and_orders():
    row = {
        "press_release_material": f"{PR} [EXISTS_UNBOUND] ; {PR}",  # dup collapses
        "court_order_material": f"{HOST}/court_order/special.078-CR-0042 [BOUND]",
        "abhiyog_ag_material": "",
    }
    assert candidates_from_row(row) == [
        ("ciaa_press_release", "2037"),
        ("court_order", "special.078-cr-0042"),
    ]


# ---------------------------------------------------------------------------
# merge_evidence -- append-only, dedupe, preserve order (whole-list replace is
# destructive, so an existing entry must never be dropped or reordered).
# ---------------------------------------------------------------------------


def test_merge_evidence_appends_new_preserving_order():
    current = [{"material_iri": PR, "additional_details": ""}]
    merged = merge_evidence(current, [(CO, "")])
    assert [e["material_iri"] for e in merged] == [PR, CO]


def test_merge_evidence_is_idempotent_on_existing():
    current = [{"material_iri": PR, "additional_details": "note"}]
    assert merge_evidence(current, [(PR, "")]) == current


# ---------------------------------------------------------------------------
# missing_candidates -- the shared "still needs binding" predicate the ledger
# and binder both use, so they cannot drift on what "bound" means.
# ---------------------------------------------------------------------------


def test_missing_candidates_returns_the_unbound_ones():
    case = {"evidence": [{"material_iri": PR, "additional_details": ""}]}
    cands = [("ciaa_press_release", "2037"), ("court_order", "special.078-cr-0042")]
    # PR already bound; only the court order is still missing.
    assert missing_candidates(case, cands) == [("court_order", "special.078-cr-0042")]


def test_missing_candidates_empty_when_fully_bound():
    case = {"evidence": [{"material_iri": PR, "additional_details": ""},
                         {"material_iri": CO, "additional_details": ""}]}
    cands = [("ciaa_press_release", "2037"), ("court_order", "special.078-cr-0042")]
    assert missing_candidates(case, cands) == []   # ledger skips this case


def test_missing_candidates_does_not_skip_on_unrelated_evidence():
    # A case with a *news* item bound but no PR must NOT be treated as done --
    # this is the over-filter bug the shared predicate exists to prevent.
    case = {"evidence": [{"material_iri": "https://jawafdehi.org/material/news/9",
                          "additional_details": ""}]}
    cands = [("ciaa_press_release", "2037")]
    assert missing_candidates(case, cands) == [("ciaa_press_release", "2037")]


# ---------------------------------------------------------------------------
# plan_case -- the four outcomes.
# ---------------------------------------------------------------------------


def test_plan_case_would_patch_on_empty_draft():
    api = FakeApi(exists={"ciaa_press_release/2037"})
    case = {"slug": "c", "state": "DRAFT", "evidence": []}
    plan = plan_case(api, case, "etag-1", [("ciaa_press_release", "2037")])
    assert plan.action == "WOULD_PATCH"
    assert plan.added == [PR]
    assert plan.if_match == "etag-1"
    assert plan.patch_body == [{"op": "replace", "path": "/evidence",
                                "value": [{"material_iri": PR, "additional_details": ""}]}]


def test_plan_case_skips_non_draft_without_probing():
    api = FakeApi()  # any probe would 404; none should happen
    case = {"slug": "c", "state": "PUBLISHED", "evidence": []}
    plan = plan_case(api, case, "e", [("ciaa_press_release", "2037")])
    assert plan.action == "SKIP_STATE"
    assert plan.patch_items == []
    assert plan.probes == []  # a skipped case is never probed


def test_plan_case_records_probe_detail_for_the_log():
    api = FakeApi(exists={"ciaa_press_release/2037"})
    case = {"slug": "c", "state": "DRAFT", "evidence": []}
    plan = plan_case(api, case, "e", [("ciaa_press_release", "2037")])
    assert len(plan.probes) == 1
    pr = plan.probes[0]
    assert (pr.source, pr.status, pr.verdict) == ("ciaa_press_release", 200, True)
    assert pr.path == "/materials/ciaa_press_release/2037/"


def test_plan_case_noop_when_already_bound():
    api = FakeApi(exists={"ciaa_press_release/2037"})
    case = {"slug": "c", "state": "DRAFT",
            "evidence": [{"material_iri": PR, "additional_details": ""}]}
    plan = plan_case(api, case, "e", [("ciaa_press_release", "2037")])
    assert plan.action == "NOOP"


def test_plan_case_drops_absent_but_binds_the_present_one():
    api = FakeApi(exists={"ciaa_press_release/2037"},
                  absent={"court_order/special.078-cr-9999"})
    case = {"slug": "c", "state": "DRAFT", "evidence": []}
    plan = plan_case(api, case, "e", [
        ("ciaa_press_release", "2037"),
        ("court_order", "special.078-cr-9999"),
    ])
    assert plan.action == "WOULD_PATCH"
    assert plan.added == [PR]
    assert plan.dropped == [f"{HOST}/court_order/special.078-cr-9999"]


def test_plan_case_aborts_on_uncertain_never_partial():
    api = FakeApi(exists={"ciaa_press_release/2037"},
                  uncertain={"court_order/special.078-cr-0042"})
    case = {"slug": "c", "state": "DRAFT", "evidence": []}
    plan = plan_case(api, case, "e", [
        ("ciaa_press_release", "2037"),
        ("court_order", "special.078-cr-0042"),
    ])
    assert plan.action == "ABORT_UNCERTAIN"
    assert plan.patch_items == []   # nothing written on uncertainty
    assert plan.uncertain == [CO]


# ---------------------------------------------------------------------------
# apply_plan -- guarded, conditional on If-Match.
# ---------------------------------------------------------------------------


def test_apply_plan_calls_replace_list_with_if_match():
    api = FakeApi()
    plan = BindPlan(slug="c", action="WOULD_PATCH", if_match="etag-7",
                    patch_items=[{"material_iri": PR, "additional_details": ""}])
    apply_plan(api, plan)
    assert api.replaced == [("c", "evidence",
                             [{"material_iri": PR, "additional_details": ""}], "etag-7")]


@pytest.mark.parametrize("action", ["NOOP", "SKIP_STATE", "ABORT_UNCERTAIN"])
def test_apply_plan_refuses_non_would_patch(action):
    with pytest.raises(ValueError):
        apply_plan(FakeApi(), BindPlan(slug="c", action=action))


def test_apply_plan_fails_closed_on_missing_etag():
    # No ETag -> If-Match absent -> replace_list would be an UNCONDITIONAL
    # destructive whole-list replace. Refuse before any write.
    api = FakeApi()
    plan = BindPlan(slug="c", action="WOULD_PATCH", if_match=None,
                    patch_items=[{"material_iri": PR, "additional_details": ""}])
    with pytest.raises(RuntimeError, match="no ETag"):
        apply_plan(api, plan)
    assert api.replaced == []  # nothing written


# ---------------------------------------------------------------------------
# run() -- dry-run must NOT write; --apply must write via replace_list.
# ---------------------------------------------------------------------------


def _args(dry_run, tmp_path):
    return types.SimpleNamespace(
        dry_run=dry_run, verbose=False, api_base_url="http://127.0.0.1:48010",
        api_token="", allow_remote_writes=False, report="", batch_csv="",
    )


def _draft_case_api():
    api = FakeApi(exists={"ciaa_press_release/2037"},
                  cases={"c": ({"slug": "c", "state": "DRAFT", "evidence": []}, "etag-1")})
    rows = [{"slug": "c", "press_release_material": PR}]
    return api, rows


def test_run_dry_run_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api, rows = _draft_case_api()
    stats, plans = run(_args(dry_run=True, tmp_path=tmp_path), api=api, rows=rows)
    assert api.replaced == []            # nothing written
    assert stats.get("WOULD_PATCH") == 1
    assert plans[0].action == "WOULD_PATCH"


def test_run_apply_writes_via_replace_list(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api, rows = _draft_case_api()
    stats, _ = run(_args(dry_run=False, tmp_path=tmp_path), api=api, rows=rows)
    assert len(api.replaced) == 1
    slug, path, items, if_match = api.replaced[0]
    assert (slug, path, if_match) == ("c", "evidence", "etag-1")
    assert stats.get("APPLIED") == 1


# ---------------------------------------------------------------------------
# Auth wiring: a bare loopback bind (no token) must authenticate via local
# DEV_AUTH Basic, exactly like convert.py and every enricher. Before this,
# _build_api was token-only -> CaseworkApi raised "exactly one of token/basic"
# on every local run, and a local DEV_AUTH server rejects Bearer anyway.
# ---------------------------------------------------------------------------


def test_build_api_uses_basic_auth_when_no_token(monkeypatch):
    monkeypatch.setenv("CASEWORK_API_USER", "abgen")
    monkeypatch.setenv("CASEWORK_API_PASSWORD", "secret")
    args = types.SimpleNamespace(
        api_base_url="http://127.0.0.1:48010", api_token="", allow_remote_writes=False)
    api = _build_api(args)
    assert api.basic == ("abgen", "secret")
    assert api.token is None


def test_build_api_no_token_and_no_creds_fails_loud(monkeypatch):
    monkeypatch.delenv("CASEWORK_API_USER", raising=False)
    monkeypatch.delenv("CASEWORK_API_PASSWORD", raising=False)
    args = types.SimpleNamespace(
        api_base_url="http://127.0.0.1:48010", api_token="", allow_remote_writes=False)
    with pytest.raises(SystemExit):
        _build_api(args)


def test_build_api_prefers_bearer_when_token_given(monkeypatch):
    # A token must not trigger the Basic path even if creds are also present.
    monkeypatch.setenv("CASEWORK_API_USER", "abgen")
    monkeypatch.setenv("CASEWORK_API_PASSWORD", "secret")
    args = types.SimpleNamespace(
        api_base_url="https://api.jawafdehi.org", api_token="tok-123",
        allow_remote_writes=False)
    api = _build_api(args)
    assert api.token == "tok-123"
    assert api.basic is None


def test_run_skips_published_case_even_on_apply(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api = FakeApi(exists={"ciaa_press_release/2037"},
                  cases={"p": ({"slug": "p", "state": "PUBLISHED", "evidence": []}, "e")})
    rows = [{"slug": "p", "press_release_material": PR}]
    stats, _ = run(_args(dry_run=False, tmp_path=tmp_path), api=api, rows=rows)
    assert api.replaced == []            # PUBLISHED never written, even with --apply
    assert stats.get("SKIP_STATE") == 1


def test_run_apply_fails_closed_when_case_has_no_etag(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    # Server returned no ETag: apply must fail closed, not write unconditionally.
    api = FakeApi(exists={"ciaa_press_release/2037"},
                  cases={"c": ({"slug": "c", "state": "DRAFT", "evidence": []}, None)})
    rows = [{"slug": "c", "press_release_material": PR}]
    stats, _ = run(_args(dry_run=False, tmp_path=tmp_path), api=api, rows=rows)
    assert api.replaced == []            # no unguarded destructive write
    assert stats.get("APPLY_FAILED") == 1


# ---------------------------------------------------------------------------
# Ledger interop: the emitted events must carry a `ts` + a ledger-vocabulary
# `status` so casework/ledger.py folds bind runs alongside the enrichers.
# ---------------------------------------------------------------------------


def test_ledger_status_maps_outcomes_to_ledger_vocabulary():
    # APPLIED is the only real change; a dry-run WOULD_PATCH is NOT (-> planned,
    # which the ledger ignores).
    assert _ledger_status("WOULD_PATCH", "APPLIED") == "enriched"
    assert _ledger_status("WOULD_PATCH", "WOULD_PATCH") == "planned"
    assert _ledger_status("NOOP", "NOOP") == "already"
    assert _ledger_status("SKIP_STATE", "SKIP_STATE") == "skipped"
    assert _ledger_status("ABORT_UNCERTAIN", "ABORT_UNCERTAIN") == "unmet"
    assert _ledger_status("WOULD_PATCH", "APPLY_FAILED") == "error"
    assert _ledger_status("-", "FETCH_FAILED") == "error"


def _read_events(tmp_path):
    (path,) = glob.glob(os.path.join(str(tmp_path), "*.events.jsonl"))
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_run_emits_ledger_compatible_event(tmp_path, monkeypatch):
    # A dry-run event must carry ts + status so the ledger can read it; the
    # status is "planned" (a dry run changed nothing), so the ledger folds it
    # into NO outcome -- exactly what an audit-of-changes should show.
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api, rows = _draft_case_api()
    run(_args(dry_run=True, tmp_path=tmp_path), api=api, rows=rows)
    events = _read_events(tmp_path)
    assert len(events) == 1
    ev = events[0]
    assert ev["ts"] and ev["stage"] == "bind" and ev["slug"] == "c"
    assert ev["status"] == "planned"     # dry run: not a ledger outcome
    assert ev["action"] == "WOULD_PATCH"  # rich audit field still present


def test_run_apply_emits_enriched_status(tmp_path, monkeypatch):
    monkeypatch.setenv("CASEWORK_RUN_LOG_DIR", str(tmp_path))
    api, rows = _draft_case_api()
    run(_args(dry_run=False, tmp_path=tmp_path), api=api, rows=rows)
    (ev,) = _read_events(tmp_path)
    assert ev["status"] == "enriched" and ev["final"] == "APPLIED"
