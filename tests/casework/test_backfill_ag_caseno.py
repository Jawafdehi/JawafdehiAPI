# tests/casework/test_backfill_ag_caseno.py
import json
import types
import urllib.error

from casework.backfill_ag_caseno import (
    APPLIED, CONFLICT_QUARANTINE, GET_ERROR, GONE, GUARD_FAIL,
    SKIP_IDEMPOTENT, WOULD_APPLY, check_guards, merge_case_no, plan_one, run,
    select_targets,
)

CASE_NO = "jawafdehi:caseNumber"


def doc(**over):
    """A stored AG material as the detail endpoint returns it."""
    base = {
        "@id": "https://jawafdehi.org/material/ag/83475",
        "name": {"ne": "जिल्ला कालिकोट"},
        "text": {"ne": "आरोप पत्र"},
        "associatedMedia": [{"@type": "MediaObject", "contentUrl": "x.pdf"}],
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


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def test_select_targets_excludes_absent_and_unrecoverable():
    rows = [row(), row(id=2, in_lake="false"), row(id=3, case_number="")]
    targets, skipped = select_targets(rows)
    assert [t["id"] for t in targets] == [83475]
    assert skipped == {"not_in_lake": 1, "unrecoverable": 1}


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------

def test_guards_pass_on_matching_special_office_doc():
    ok, guards = check_guards(doc(), 83475)
    assert ok is True
    assert guards["officeLevel"] == 1


def test_guards_reject_record_id_mismatch():
    # Would otherwise stamp a case number onto a DIFFERENT document.
    ok, _ = check_guards(doc(**{"jawafdehi:recordId": "999"}), 83475)
    assert ok is False


def test_guards_reject_non_special_office_and_wrong_source_type():
    assert check_guards(doc(**{"jawafdehi:officeLevel": 3}), 83475)[0] is False
    assert check_guards(doc(**{"jawafdehi:sourceType": "OTHER"}), 83475)[0] is False


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
    assert merged["jawafdehi:caseNumberAlt"] == "081-CR-0009"
    assert len(added) == 4


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
    assert plan_one(FakeApi({}), row())["outcome"] == GONE
    api = FakeApi({"83475": doc()}, error=urllib.error.URLError("boom"))
    assert plan_one(api, row())["outcome"] == GET_ERROR


# --------------------------------------------------------------------------
# run() -- dry-run must never write
# --------------------------------------------------------------------------

def _args(map_path, **over):
    base = dict(map=str(map_path), dry_run=True, limit=0, verbose=False,
                put_bodies="", write_interval=0, api_token="tok",
                api_base_url="http://127.0.0.1:48010", allow_remote_writes=False)
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
