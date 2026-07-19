#!/usr/bin/env python
"""Three-way A/B harness: Arm A (donor) vs Arm B (port) vs Arm G (golden).

WHAT THIS RUNS AND WHY IT IS SAFE

Every write goes to the LOCAL sqlite stack behind `http://127.0.0.1:48010`.
Production (`https://api.jawafdehi.org`) is never contacted. Before any
`--apply` run the harness confirms the process listening on the target port
is owned by THIS uid (port 48000 on this host belongs to another OS user and
has returned misleading 200s in the past), and it refuses to run otherwise.

WHY THE DATABASE IS RESTORED BETWEEN ARMS

The two arms cannot share a database. `enrich_tags` classifies from case
metadata that the OTHER stages write (`bigo`, `key_allegations`), so letting
Arm A run first would change Arm B's INPUTS and the second arm would be
measured on a case the first arm had already rewritten. So each arm starts
from a byte-identical restored copy of the same seeded baseline. Restores
bracket a server stop/start because the dev server holds open sqlite
handles, and swapping files underneath a live connection yields stale reads
or a malformed image.

WHY BOTH ARMS RUN THE SAME MODEL

Arm B forces `claude_cli` + haiku on BOTH tiers via
`casework.common.llm.dev_env_overrides`. Arm A's own `bootstrap()` sets the
identical four environment variables when invoked with
`--provider claude_cli --model haiku` (it defaults to `proxy`, so passing
these explicitly is mandatory, not cosmetic). Model quality therefore cancels
out and a divergence is attributable to enricher logic.

ARM INVOCATION DIFFERS BY DESIGN

Arm A (donor) WRITES BY DEFAULT -- its `--dry-run` is opt-out. Arm B
deliberately inverted that: it is read-only unless `--apply` is passed. So
"apply" means bare `--force` for Arm A and `--force --apply` for Arm B.
Getting this backwards would silently make one arm a no-op, which is exactly
the false-parity failure this project keeps catching.

ENTITIES ARE COMPARED ON EXTRACTION ONLY

The donor's entity write path (`create_entity(display_name)` -> flat-id
PATCH) 400s against today's `EntityPatchItemSerializer`, which requires a
canonical NES `@id` IRI; the port is deliberately extraction-only for that
reason. So the entities stage is compared on what each arm EXTRACTED (parsed
from stdout), never on what it wrote.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from casework.ab.diff import compare_field, three_way_report  # noqa: E402
from casework.ab.sample import select_sample, stratum  # noqa: E402

DB_FILES = ("db.sqlite3", "db_nes.sqlite3", "db_ngm.sqlite3")

# stage -> (module, case field it writes). `entities` writes nothing on the
# port side and cannot be compared on writes at all (see module docstring).
STAGES = {
    "bigo": ("enrich_missing_bigo", "bigo"),
    "tags": ("enrich_tags", "tags"),
    "timeline": ("enrich_timeline", "timeline"),
    "allegations": ("enrich_allegations", "key_allegations"),
    "entities": ("enrich_related_entities", None),
}
COMPARE_FIELDS = {
    "bigo": "bigo",
    "tags": "tags",
    "timeline": "timeline",
    "allegations": "key_allegations",
    "entities": "entities",
}


# ---------------------------------------------------------------- safety ---


def listening_pid(port):
    """PID listening on `port`, or None."""
    out = subprocess.run(
        ["ss", "-ltnp"], capture_output=True, text=True, check=False).stdout
    for line in out.splitlines():
        if f":{port} " in line:
            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
    return None


def owner_uid(pid):
    """Owning uid of `pid`. Separate function so tests can stub it without
    monkeypatching `os.stat` globally (which breaks pytest's own internals)."""
    return os.stat(f"/proc/{pid}").st_uid


def assert_port_is_ours(port):
    """Refuse to write unless the listener on `port` belongs to this uid.

    A 200 from a shared host proves the port answers, NOT whose server
    answered. Writing enrichment data into another user's Django instance
    would be a serious error, so this is a hard gate, never a warning.
    """
    pid = listening_pid(port)
    if pid is None:
        raise SystemExit(f"REFUSING: nothing is listening on port {port}")
    try:
        uid = owner_uid(pid)
    except FileNotFoundError:
        raise SystemExit(f"REFUSING: pid {pid} on port {port} vanished")
    if uid != os.getuid():
        raise SystemExit(
            f"REFUSING: pid {pid} on port {port} is owned by uid "
            f"{uid}, not us ({os.getuid()}). Do not touch it.")
    return pid


# --------------------------------------------------------- server + dbs ---


def stop_server(port):
    pid = listening_pid(port)
    if pid is None:
        return
    assert_port_is_ours(port)
    subprocess.run(["kill", str(pid)], check=False)
    for _ in range(50):
        if listening_pid(port) is None:
            return
        time.sleep(0.2)
    subprocess.run(["kill", "-9", str(pid)], check=False)
    time.sleep(1)


def start_server(repo, port, log_path):
    env = dict(os.environ, DEBUG="True", DEV_AUTH="1")
    env.pop("DATABASE_URL", None)
    log = open(log_path, "ab")
    proc = subprocess.Popen(
        [sys.executable, "manage.py", "runserver", f"0.0.0.0:{port}", "--noreload"],
        cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        time.sleep(0.5)
        if listening_pid(port):
            time.sleep(1.0)
            return proc
    raise SystemExit(f"server failed to come up on {port}; see {log_path}")


def backup_dbs(repo, baseline):
    """Snapshot the seeded DB so a bad run cannot poison later comparisons."""
    os.makedirs(baseline, exist_ok=True)
    for name in DB_FILES:
        src = os.path.join(repo, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(baseline, name))


def restore_dbs(repo, baseline):
    for name in DB_FILES:
        src = os.path.join(baseline, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(repo, name))
            # sqlite sidecars from an interrupted run would resurrect state
            # that the restored main file does not contain.
            for ext in ("-wal", "-shm"):
                side = os.path.join(repo, name + ext)
                if os.path.exists(side):
                    os.remove(side)


# ------------------------------------------------------------------ api ---


def api_get(base, path, auth, timeout=120):
    req = urllib.request.Request(base + path)
    if auth:
        import base64
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def readback(base, slugs, auth):
    """Read the comparable fields back off the case DETAIL endpoint.

    DETAIL, never LIST: the list endpoint returns `material: null`.
    """
    out = {}
    for slug in slugs:
        try:
            case = api_get(base, f"/api/cases/{slug}/", auth)
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            out[slug] = {"_error": str(exc)}
            continue
        out[slug] = {
            "bigo": case.get("bigo"),
            "tags": case.get("tags") or [],
            "timeline": case.get("timeline") or [],
            "key_allegations": case.get("key_allegations") or [],
        }
    return out


# ------------------------------------------------------------ arm runner ---

# Arm B prints an explicit extraction count; Arm A prints one line per
# extracted item (dry-run) or one create-failure line per item (apply).
RE_B_ENTITIES = re.compile(r"Extracted (\d+) entities, (\d+) accused note")
RE_A_DRYRUN_ENTITY = re.compile(r"^\s+\[DRY RUN\] (location|related)\s+(.+?)(?:\s+—|$)")
RE_A_CREATE_FAIL = re.compile(r"Failed to create entity '(.+?)'")
RE_CASE_HEADER = re.compile(r"^\[(\d+)/(\d+)\]\s+(\S+)")


def header_slug(match, slugs):
    """Resolve a case-header line to a slug, or None if it cannot be trusted.

    The two arms print different things: the port prints the slug, while the
    donor prints `case.get("case_id", "?")` -- which is literally "?" on
    today's payload, since the field was dropped in the API migration. So the
    donor can only be attributed POSITIONALLY, via the `[idx/total]` counter.
    That is safe only when the arm processed exactly the cases we asked for:
    a failed fetch makes the donor skip a slug and shift every subsequent
    index, silently misattributing every later case. When `total` does not
    match the number of slugs requested, this returns None so the caller
    reports the run as unattributed instead of guessing.
    """
    idx, total, token = int(match.group(1)), int(match.group(2)), match.group(3)
    if token in slugs:
        return token
    if total != len(slugs):
        return None
    if 1 <= idx <= len(slugs):
        return slugs[idx - 1]
    return None


def parse_entities(stdout, arm, slugs=()):
    """Per-slug entity extraction counts and names, parsed from arm stdout.

    Pass `slugs` so the donor's "?" headers can be positionally attributed;
    with a single-slug invocation this is exact.
    """
    per = {}
    slug = None
    for line in stdout.splitlines():
        m = RE_CASE_HEADER.match(line)
        if m:
            slug = header_slug(m, list(slugs))
            if slug is not None:
                per.setdefault(slug, {"count": 0, "names": []})
            continue
        if slug is None:
            continue
        if arm == "B":
            m = RE_B_ENTITIES.search(line)
            if m:
                per[slug]["count"] = int(m.group(1))
                per[slug]["accused_notes"] = int(m.group(2))
                continue
            m = re.match(r"^\s{4}(location|related)\s+(.+)$", line)
            if m:
                per[slug]["names"].append(m.group(2).strip())
        else:
            m = RE_A_DRYRUN_ENTITY.match(line)
            if m:
                per[slug]["count"] += 1
                per[slug]["names"].append(m.group(2).strip())
                continue
            m = RE_A_CREATE_FAIL.search(line)
            if m:
                per[slug]["names"].append(m.group(1).strip())
    return per


def run_stage(arm, cwd, stage, slugs, base, apply_writes, model, timeout=3600):
    """Invoke one enricher for one arm over the sample slugs."""
    module, _ = STAGES[stage]
    cmd = [sys.executable, f"casework/{module}.py", "--force",
           "--provider", "claude_cli", "--model", model,
           "--api-base-url", base]
    for slug in slugs:
        cmd += ["--slug", slug]
    if arm == "B":
        # Port is read-only unless --apply.
        if apply_writes:
            cmd.append("--apply")
    else:
        # Donor WRITES BY DEFAULT; --dry-run is the opt-out.
        if not apply_writes:
            cmd.append("--dry-run")
    env = dict(os.environ, DEBUG="True", PYTHONPATH=cwd)
    env.pop("DATABASE_URL", None)
    started = time.time()
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=timeout)
    return {
        "arm": arm, "stage": stage, "cmd": " ".join(cmd[1:]),
        "returncode": proc.returncode,
        "seconds": round(time.time() - started, 1),
        "stdout": proc.stdout, "stderr": proc.stderr[-4000:],
    }


def parse_outcomes(stdout, slugs):
    """Per-slug outcome for a stage, inferred from the arm's own output.

    Reports unmet / skipped / error distinctly -- collapsing them would make
    an unreachable case look like an intentionally skipped one.
    """
    slugs = list(slugs)
    out = {s: "no-output-line" for s in slugs}
    headers = [RE_CASE_HEADER.match(ln) for ln in stdout.splitlines()]
    headers = [h for h in headers if h]
    # If the arm did not emit exactly one header per requested slug, the
    # donor's positional attribution is unsound -- say so rather than
    # mislabel every case after the first skipped fetch.
    if headers and len(headers) != len(slugs):
        return {s: "unattributed-header-mismatch" for s in slugs}
    slug = None
    for line in stdout.splitlines():
        m = RE_CASE_HEADER.match(line)
        if m:
            slug = header_slug(m, slugs)
            continue
        if slug is None or slug not in out:
            continue
        low = line.lower()
        if "unmet prerequisite" in low or "no press release or court order" in low:
            out[slug] = "unmet"
        elif "failed" in low or "error" in low:
            out[slug] = "error"
        elif "skipping" in low:
            out[slug] = "skipped"
        elif "[updated]" in low:
            out[slug] = "enriched"
        elif "[dry run]" in low or "would patch" in low:
            out[slug] = "would-enrich"
        elif "extracted" in low and out[slug] == "no-output-line":
            out[slug] = "extracted"
    return out


# --------------------------------------------------------------- compare ---


def build_rows(slugs, arm_a, arm_b, golden, entities_a, entities_b):
    """One comparison row per case per field.

    A failed READBACK is flagged explicitly. Without that flag an arm whose
    values could not be read back would look exactly like an arm that
    produced nothing -- a measurement failure silently reported as a
    behavioural result.
    """
    rows = []
    for slug in slugs:
        a, b = arm_a.get(slug, {}), arm_b.get(slug, {})
        g = golden.get(slug, {})
        readback_error = {
            arm for arm, vals in (("A", a), ("B", b)) if "_error" in vals}
        for stage, field in COMPARE_FIELDS.items():
            if stage == "entities":
                va = entities_a.get(slug, {}).get("names") or []
                vb = entities_b.get(slug, {}).get("names") or []
                vg = []
            else:
                va, vb, vg = a.get(field), b.get(field), g.get(field)
            row = compare_field(field if stage != "entities" else "entities",
                                va, vb, vg)
            row["slug"] = slug
            row["stage"] = stage
            row["readback_error"] = sorted(readback_error)
            if readback_error:
                # Not a behavioural verdict: we failed to MEASURE this row.
                row["verdict"] = "readback_error"
            rows.append(row)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arm-a", required=True)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--survey", required=True,
                    help="JSON of per-case evidence shapes (sample frame)")
    ap.add_argument("--work", required=True, help="dir for raw run artifacts")
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--base", default="http://127.0.0.1:48010")
    ap.add_argument("--port", type=int, default=48010)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", default="task-16")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--stages", default="bigo,tags,timeline,allegations,entities")
    ap.add_argument("--apply", action="store_true",
                    help="actually run the arms and write to the LOCAL db")
    args = ap.parse_args(argv)

    os.makedirs(args.work, exist_ok=True)
    baseline = os.path.join(args.work, "baseline_db")
    auth = (os.environ.get("JAWAFDEHI_API_BASIC_USER", ""),
            os.environ.get("JAWAFDEHI_API_BASIC_PASS", ""))

    survey = json.load(open(args.survey))
    sample = select_sample(survey, n=args.n, seed=args.seed)
    slugs = sample["slugs"]
    golden = json.load(open(os.path.join(args.snapshot, "golden.json")))
    stages = [s for s in args.stages.split(",") if s]

    print(f"sample: {len(slugs)} cases from a frame of {sample['frame_size']}")
    for name, members in sorted(sample["strata"].items()):
        print(f"  {name}: {len(members)}")
    if not args.apply:
        print("\n--apply not given; nothing was run. Sample above is the plan.")
        json.dump(sample, open(os.path.join(args.work, "sample.json"), "w"), indent=1)
        return 0

    pid = assert_port_is_ours(args.port)
    print(f"port {args.port} listener pid {pid} confirmed ours (uid {os.getuid()})")

    if not os.path.exists(os.path.join(baseline, "db.sqlite3")):
        stop_server(args.port)
        backup_dbs(args.repo, baseline)
        start_server(args.repo, args.port, os.path.join(args.work, "server.log"))
        print(f"baseline DB snapshot written to {baseline}")

    runs, results, entities = {}, {}, {}
    for arm, cwd in (("A", args.arm_a), ("B", args.repo)):
        print(f"\n=== ARM {arm} ({cwd}) ===")
        stop_server(args.port)
        restore_dbs(args.repo, baseline)
        start_server(args.repo, args.port, os.path.join(args.work, "server.log"))
        assert_port_is_ours(args.port)
        runs[arm] = {}
        for stage in stages:
            # Arm A's entities stage runs DRY so extraction can be counted
            # cleanly; its write path is exercised separately (it 400s).
            apply_writes = not (arm == "A" and stage == "entities")
            print(f"  {stage} ...", flush=True)
            if stage == "entities":
                # Entities are compared on EXTRACTION parsed from stdout --
                # there is no field to read back -- so each case runs in its
                # own subprocess, making attribution exact rather than
                # positional. Every other stage is attributed by per-slug DB
                # readback, which needs no parsing at all.
                merged, outcomes, secs, rcs = {}, {}, 0.0, []
                stdout_all = []
                for slug in slugs:
                    r1 = run_stage(arm, cwd, stage, [slug], args.base,
                                   apply_writes, args.model)
                    merged.update(parse_entities(r1["stdout"], arm, [slug]))
                    outcomes.update(parse_outcomes(r1["stdout"], [slug]))
                    secs += r1["seconds"]
                    rcs.append(r1["returncode"])
                    stdout_all.append(f"##### {slug}\n{r1['stdout']}")
                r = {"arm": arm, "stage": stage, "cmd": "per-slug",
                     "returncode": max(rcs) if rcs else 0,
                     "seconds": round(secs, 1),
                     "stdout": "\n".join(stdout_all), "stderr": ""}
                entities[arm] = merged
                r["outcomes"] = outcomes
            else:
                r = run_stage(arm, cwd, stage, slugs, args.base, apply_writes,
                              args.model)
                r["outcomes"] = parse_outcomes(r["stdout"], slugs)
            runs[arm][stage] = r
            print(f"    rc={r['returncode']} {r['seconds']}s")
            with open(os.path.join(args.work, f"arm{arm}_{stage}.log"), "w") as fh:
                fh.write(r["stdout"] + "\n--- STDERR ---\n" + r["stderr"])
        results[arm] = readback(args.base, slugs, auth)

    # leave the local DB back on the pristine baseline
    stop_server(args.port)
    restore_dbs(args.repo, baseline)
    start_server(args.repo, args.port, os.path.join(args.work, "server.log"))

    rows = build_rows(slugs, results["A"], results["B"], golden,
                      entities.get("A", {}), entities.get("B", {}))
    report = three_way_report(rows)
    payload = {
        "sample": sample, "rows": rows, "report": report,
        "results": results, "entities": entities,
        "outcomes": {a: {s: runs[a][s]["outcomes"] for s in runs[a]} for a in runs},
        "timings": {a: {s: runs[a][s]["seconds"] for s in runs[a]} for a in runs},
        "returncodes": {a: {s: runs[a][s]["returncode"] for s in runs[a]} for a in runs},
        "strata": {s: stratum(survey[s]) for s in slugs},
    }
    json.dump(payload, open(os.path.join(args.work, "ab_raw.json"), "w"),
              indent=1, ensure_ascii=False)
    print(f"\nraw results -> {os.path.join(args.work, 'ab_raw.json')}")
    print(json.dumps(report["counts"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
