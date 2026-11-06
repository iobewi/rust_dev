#!/usr/bin/env python3
"""Independent comparator of qualification evidence (read-only).

Reads existing evidence directories, never writes inside them, checks them against acceptance criteria written
BEFORE the runs (qualification_criteria.json) and produces a requirement -> scenario -> verdict -> evidence matrix.

Rules that are never relaxed:
  * missing or unverifiable evidence NEVER produces a PASS: a recorded PASS that cannot be verified is reported
    INDETERMINATE (reason listed); a contradicted PASS is reported INDETERMINATE and flagged CONTRADICTED;
  * recorded negative or inconclusive verdicts (FAIL, BLOCKED, INDETERMINATE, SKIP) are kept as they were
    recorded - historical results are listed, never reinterpreted or hidden;
  * flash dumps, backups and support files are re-hashed; provenance (Git SHA, clean tree, versions) is checked.

Usage:
  qualification_compare.py --criteria qualification_criteria.json --output DIR \
      --root historic:historic=PATH --root repeat:candidate=PATH [--criteria-sha256 SHA] [--strict]
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

TOOL_VERSION = "1"
STATUSES = ("PASS", "FAIL", "SKIP", "BLOCKED", "INDETERMINATE")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
GRANT_OK_LINE = re.compile(r"^(?:\d+)")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Check:
    """outcome: OK | FAIL | MISSING (evidence absent or unverifiable). severity: required | advisory."""
    def __init__(self, name, outcome, detail="", severity="required"):
        self.name, self.outcome, self.detail, self.severity = name, outcome, detail, severity

    def as_dict(self):
        return {"name": self.name, "outcome": self.outcome, "severity": self.severity, "detail": self.detail}


class Run:
    """Lazy, read-only view of one evidence directory."""
    def __init__(self, path):
        self.path = Path(path)
        self._cache = {}

    def exists(self, name):
        return (self.path / name).is_file()

    def json(self, name):
        if name not in self._cache:
            try:
                self._cache[name] = json.loads((self.path / name).read_text())
            except (OSError, ValueError) as error:
                self._cache[name] = error
        value = self._cache[name]
        return None if isinstance(value, Exception) else value

    def jsonl(self, name):
        key = "jsonl:" + name
        if key not in self._cache:
            rows = []
            try:
                for line in (self.path / name).read_text().splitlines():
                    if line.strip():
                        rows.append(json.loads(line))
                self._cache[key] = rows
            except (OSError, ValueError) as error:
                self._cache[key] = error
        value = self._cache[key]
        return None if isinstance(value, Exception) else value

    def report_test(self):
        report = self.json("report.json")
        tests = (report or {}).get("tests") if isinstance(report, dict) else None
        return tests[0] if tests else None

    def events(self):
        rows = self.jsonl("events.jsonl")
        return None if rows is None else [r for r in rows if "kind" in r]

    def operations(self):
        """Last record of each operation, across every board-N/operations.jsonl, in first-seen order."""
        last, order = {}, []
        for path in sorted(self.path.glob("board-*/operations.jsonl")):
            try:
                for line in path.read_text().splitlines():
                    if line.strip():
                        row = json.loads(line)
                        if row["id"] not in last:
                            order.append(row["id"])
                        last[row["id"]] = row
            except (OSError, ValueError, KeyError):
                return None
        return [last[i] for i in order]


# ---- common integrity / provenance / consistency checks ------------------------------------------------------------
def check_files(run, required):
    missing = [name for name in required if not run.exists(name)]
    return Check("files_present", "MISSING" if missing else "OK", "missing: " + ", ".join(missing) if missing else "")


def check_hash(name, expected, path, what):
    if not expected:
        return Check(name, "MISSING", f"no recorded digest for {what}")
    if not Path(path).is_file():
        return Check(name, "MISSING", f"{what} absent")
    actual = sha256_file(path)
    return Check(name, "OK" if actual == expected else "FAIL", "" if actual == expected else f"{what}: recorded {expected[:12]} != actual {actual[:12]}")


def common_checks(run, criteria, scenario, recorded):
    checks = [check_files(run, criteria["common"]["required_files"] + criteria["scenarios"].get(scenario, {}).get("extra_files", [])
                          if recorded == "PASS" else criteria["common"]["required_files"])]
    meta, provenance, manifest = run.json("run.json"), run.json("provenance.json"), run.json("manifest.json")
    checks.append(check_hash("recipe_hash", (meta or {}).get("recipe_sha256"), run.path / "recipe.py", "recipe.py") if meta else
                  Check("recipe_hash", "MISSING", "run.json unreadable"))
    support = (provenance or {}).get("support_sha256")
    if not support:
        checks.append(Check("support_hashes", "MISSING", "no support_sha256 in provenance.json"))
    else:
        bad = [n for n, d in support.items() if not (run.path / "support" / n).is_file() or sha256_file(run.path / "support" / n) != d]
        checks.append(Check("support_hashes", "FAIL" if bad else "OK", "differs or absent: " + ", ".join(bad) if bad else f"{len(support)} files"))
    sources = (meta or {}).get("client_source_sha256")
    if not sources:
        checks.append(Check("client_source_hashes", "MISSING", "no client_source_sha256"))
    else:
        bad = [n for n, d in sources.items() if not (run.path / n).is_file() or sha256_file(run.path / n) != d]
        checks.append(Check("client_source_hashes", "FAIL" if bad else "OK", ", ".join(bad)))
    checks.append(check_hash("qualification_module_hash", (meta or {}).get("support_sha256"), run.path / "qualification.py", "qualification.py"))
    if manifest is None or not (meta or {}).get("manifest_sha256"):
        checks.append(Check("manifest_hash", "MISSING", "manifest.json or its digest absent"))
    else:
        actual = canonical_sha(manifest)
        checks.append(Check("manifest_hash", "OK" if actual == meta["manifest_sha256"] else "FAIL",
                            "" if actual == meta["manifest_sha256"] else f"recorded {meta['manifest_sha256'][:12]} != canonical {actual[:12]}"))
    if provenance is None:
        checks.append(Check("provenance", "MISSING", "provenance.json absent (run predates the provenance contract)"))
    else:
        problems = []
        if not HEX40.match(str(provenance.get("git_sha", ""))):
            problems.append("no 40-hex Git SHA")
        if provenance.get("clean") is not True:
            problems.append("sources were not clean")
        if provenance.get("dirty_allowed_by_option"):
            problems.append("--qual-allow-dirty was used")
        checks.append(Check("provenance", "FAIL" if problems else "OK", "; ".join(problems)))
    before, after = run.json("before.json"), run.json("after.json")
    versions = [(meta or {}).get(k) for k in ("python", "pytest", "iobewi-bench")]
    server = ((before or {}).get("server_observed") or {})
    agent = (((before or {}).get("device") or {}).get("provenance") or {}).get("agent_sha256")
    missing = [n for n, v in (("python", versions[0]), ("pytest", versions[1]), ("iobewi-bench", versions[2]),
                              ("server git_sha", server.get("git_sha")), ("server_sha256", server.get("server_sha256")),
                              ("agent_sha256", agent)) if not v]
    checks.append(Check("versions_recorded", "MISSING" if missing else "OK", "missing: " + ", ".join(missing) if missing else ""))
    test, cleanup = run.report_test(), run.json("cleanup.json")
    verdicts = run.jsonl("verdicts.jsonl")
    if test is None or cleanup is None:
        checks.append(Check("status_consistency", "MISSING", "report.json or cleanup.json unreadable"))
    else:
        problems = []
        if test.get("status") not in STATUSES:
            problems.append(f"unknown status {test.get('status')!r}")
        if recorded == "PASS" and test.get("incidents"):
            problems.append("PASS with recorded incidents")
        if recorded == "PASS" and cleanup.get("errors"):
            problems.append("PASS with cleanup errors")
        if verdicts and (verdicts[-1].get("id") != test.get("id") or verdicts[-1].get("status") != test.get("status")):
            problems.append("verdicts.jsonl disagrees with report.json")
        checks.append(Check("status_consistency", "FAIL" if problems else "OK", "; ".join(problems)))
    if recorded == "PASS":
        device = (after or {}).get("device") if after else None
        if device is None:
            checks.append(Check("final_state", "MISSING", "after.json unreadable"))
        else:
            problems = [n for n, bad in (("not connected", not device.get("connected")), ("quarantined", device.get("quarantined")),
                                         ("reservation left", device.get("reservation") is not None),
                                         ("grant still active", device.get("privileged_seconds") != 0)) if bad]
            checks.append(Check("final_state", "FAIL" if problems else "OK", "; ".join(problems)))
        checks.append(continuity_check(before, after, criteria["scenarios"].get(scenario, {}).get("continuity", "unchanged")))
    checks.append(flash_checks(run))
    return checks


def continuity_check(before, after, expected):
    if not before or not after:
        return Check("continuity", "MISSING", "before.json or after.json unreadable")
    gen_changed = before["device"].get("generation") != after["device"].get("generation")
    epoch_changed = before.get("epoch") != after.get("epoch")
    if expected == "unchanged":
        bad = [n for n, c in (("generation changed", gen_changed), ("epoch changed", epoch_changed)) if c]
    else:
        bad = [n for n, c in (("epoch did not change", not epoch_changed), ("generation did not change", not gen_changed)) if c]
    return Check("continuity", "FAIL" if bad else "OK", "; ".join(bad))


def flash_checks(run):
    problems, count = [], 0
    for sidecar in sorted(run.path.glob("board-*/*.json")):
        blob = sidecar.with_suffix(".bin")
        meta = run.json(str(sidecar.relative_to(run.path)))
        if not isinstance(meta, dict) or "sha256" not in meta:
            continue
        count += 1
        if not blob.is_file():
            problems.append(f"{sidecar.name}: blob absent")
        elif sha256_file(blob) != meta["sha256"] or blob.stat().st_size != meta.get("size", blob.stat().st_size):
            problems.append(f"{sidecar.name}: blob digest or size differs from its sidecar")
        elif meta.get("complete") is False:
            problems.append(f"{sidecar.name}: marked incomplete")
    return Check("flash_sidecars", "FAIL" if problems else "OK", "; ".join(problems) if problems else f"{count} sidecars re-hashed")


# ---- scenario checks (written from the acceptance criteria) --------------------------------------------------------
def grant_events(run):
    events = run.events()
    return None if events is None else [(e["at"], e["seconds"]) for e in events if e.get("kind") == "grant"]


def refusals(run):
    rows = run.jsonl("requests.jsonl")
    return None if rows is None else [r for r in rows if r.get("ok") is None and r.get("status")]


def evidence_lines(run):
    test = run.report_test()
    return None if test is None else test.get("evidence", [])


def has_evidence(lines, pattern):
    return lines is not None and any(re.search(pattern, line) for line in lines)


def scenario_checks(run, scenario, params):
    lines, ops = evidence_lines(run), run.operations()
    checks = []

    def add(name, ok, detail="", missing=False):
        checks.append(Check(name, "MISSING" if missing else ("OK" if ok else "FAIL"), detail))

    def ev(name, pattern, detail):
        """The recipe's own evidence line is the only witness: absent = cannot verify (never a PASS, but not a contradiction)."""
        checks.append(Check(name, "OK" if has_evidence(lines, pattern) else "MISSING", detail))
    if "allowed_commands" in params:
        if ops is None:
            add("commands_allowed", False, "operations unreadable", missing=True)
        else:
            extra = sorted({o["command"] for o in ops} - set(params["allowed_commands"]))
            add("commands_allowed", not extra and bool(ops), "unexpected commands: " + ", ".join(extra) if extra else ("" if ops else "no operation recorded"))
    grants, refused = grant_events(run), refusals(run)
    expected = params.get("operator_requests")
    if expected is not None:
        requests = run.jsonl("operator-requests.jsonl") if run.exists("operator-requests.jsonl") else []
        if requests is None or (not requests and sum(expected.values())):
            add("operator_gestures_traced", False, "operator-requests.jsonl missing or empty although the scenario needs operator gestures", missing=True)
        else:
            def kind(text):                       # by LEADING verb: a grant request may mention "the revoke request" later in its text
                head = text.strip().upper()
                return "revoke" if head.startswith("REVOKE") else "grant" if head.startswith("GRANT") else "other"
            kinds = [kind(r.get("request", "")) for r in requests]
            seen = {k: kinds.count(k) for k in ("grant", "revoke", "other")}
            positives = sum(1 for _, s in (grants or []) if s > 0)
            zeros = sum(1 for _, s in (grants or []) if s == 0)
            problems = [f"{k}: requested {seen[k]}, expected {expected[k]}" for k in seen if seen[k] != expected[k]]
            if grants is not None and scenario != "Q3":
                if positives != seen["grant"]:
                    problems.append(f"{positives} grant event(s) observed for {seen['grant']} grant request(s)")
                if zeros > seen["revoke"]:
                    problems.append(f"{zeros} revocation event(s) observed for {seen['revoke']} revoke request(s)")
            add("operator_gestures_traced", not problems, "; ".join(problems))
    if scenario == "Q1":
        a, b = run.json("board-0/ref-a.json"), run.json("board-0/ref-b.json")
        if not a or not b:
            add("backups_equal", False, "ref-a / ref-b sidecars unreadable", missing=True)
        else:
            add("backups_equal", a.get("sha256") == b.get("sha256") and a.get("complete") and b.get("complete") and a.get("size") == b.get("size"),
                "the two backups differ or are incomplete")
            regions = (run.json("manifest.json") or {}).get("write_regions") or []
            add("region_matches_manifest", len(regions) == 1 and (regions[0]["address"], regions[0]["size"]) == (a["address"], a["size"]),
                "backup region differs from the approved manifest region")
        ev("region_disjoint_recorded", r"region disjoint from table and partitions", "no disjointness evidence")
        ev("pattern_readback_recorded", r"programmed; independent readback equal to pattern sha256=[0-9a-f]{64}", "no independent readback evidence")
        test = run.report_test()
        restored = str((test or {}).get("restoration", "")).startswith("restored; independent compare equal")
        checks.append(Check("restored_and_compared", "OK" if restored else "MISSING", "restoration not proven"))
        add("grant_revoked_locally", bool(grants) and grants[-1][1] == 0 and any(s > 0 for _, s in grants), "no positive grant followed by a zero (revocation)", grants is None)
    if scenario in ("Q2a", "Q2b", "Q2c", "Q2d"):
        if grants is None:
            add("grant_timeline", False, "events unreadable", missing=True)
        elif scenario == "Q2a":
            add("natural_expiry", len(grants) == 1 and grants[0][1] > 0, f"grant events: {[s for _, s in grants]} (expiry means one positive event and no zero)")
        elif scenario == "Q2b":
            ok = len(grants) == 2 and grants[0][1] > 0 and grants[1][1] == 0
            left = grants[0][1] - (grants[1][0] - grants[0][0]) if ok else None
            add("local_revocation", ok and left > params["min_remaining_s"], "not a revocation (zero event missing, or it came at/after expiry)" if not ok else f"only {left:.0f}s left")
        if "refusal" in params:
            status, code = params["refusal"]
            hits = [r for r in (refused or []) if (r["status"], r.get("code")) == (status, code)]
            add("refusal_code", bool(hits), f"expected {status} {code}; saw {[(r['status'], r.get('code')) for r in refused or []]}", refused is None)
            if scenario in ("Q2c", "Q2d") and hits and grants is not None:
                moment = hits[0]["at"]
                before = [(t, sec) for t, sec in sorted(grants) if t < moment]
                valid = bool(before) and before[-1][1] > 0 and before[-1][1] - (moment - before[-1][0]) > 0
                add("refused_with_valid_grant", valid, "the refusal is not inside a valid grant window (could be a grant refusal)")
                who = hits[0]["client"]
                expected = params["refusing_client"]
                add("refusing_client", (who == "primary") == (expected == "primary"), f"refused client {who!r}, expected {expected!r}")
            if scenario == "Q2d":
                cleanup = run.json("cleanup.json") or {}
                add("distinct_clients", len(cleanup.get("submissions", {})) >= 2, "fewer than two distinct clients submitted commands", not cleanup)
        if scenario in ("Q2a", "Q2b"):
            terminal = [o for o in (ops or []) if o["command"] == "trigger" and o["state"] == "completed" and o["settled"]]
            add("owner_disarm_settled", len(terminal) >= 2, "arm and disarm should both be settled", ops is None)
    if scenario == "Q3":
        windows = run.jsonl("windows.jsonl")
        add("windows_closed_and_reopened", windows is not None and [w.get("event") for w in windows] == ["interrupted", "opened"], "proof windows not closed then reopened", windows is None)
        ev("stop_before_start_proof", r"old child exited.*before the replacement started", "no launcher ordering proof")
        ev("journal_and_lock_kept", r"same journal and lock files", "no storage continuity proof")
        ev("agent_reconnected_idle", r"agent reconnected;.*grant 0; no reservation", "no reconnection proof")
    if scenario == "C3R-0":
        ev("identity_measured_equals_declared", r"measured chip=\S+ flash_size=\d+ \(equal to the declaration\)", "no equality evidence")
    if scenario == "C3R-1":
        ev("repeated_reads_equal", r"two reads equal, sha256=[0-9a-f]{64}", "no repeated-read evidence")
        checks.append(Check("server_readbacks_released", "OK" if has_evidence(lines, r"server readback \w+ deleted after its private copy") else "FAIL",
                            "no release evidence (recipe predating the retention fix leaves artifacts on the server)", "advisory"))
    if scenario == "C3R-2":
        probes = run.json("table-probes.json")
        valid = [p for p in (probes or []) if p.get("result") == "valid"]
        add("one_valid_table", probes is not None and len(valid) == 1, f"{len(valid)} valid tables among the probes", probes is None)
        ev("table_recorded", r"one valid table at 0x[0-9a-f]+", "no table evidence")
        checks.append(Check("server_readbacks_released", "OK" if has_evidence(lines, r"server readback \w+ deleted after its private copy") else "FAIL",
                            "no release evidence (recipe predating the retention fix)", "advisory"))
    if scenario.startswith("C3R"):
        transport = run.json("transport.json")
        ok = isinstance(transport, dict) and {"declared_by_operator", "inferred_by_agent", "provenance_source"} <= set(transport)
        add("transport_declared_and_inferred", ok, "declaration and agent inference must both be recorded", transport is None)
    if scenario == "C3R-3":
        ev("rom_banner_same_window", r"'ESP-ROM:\S+' observed after the reset, same proof window", "no banner evidence")
        add("no_continuity_break", not run.exists("windows.jsonl"), "a window was interrupted: this is not a plain PASS")
    return checks


# ---- assessment, fingerprint --------------------------------------------------------------------------------------------
RUN_DIR = re.compile(r"^(?P<scenario>.+)-\d{8}T\d{6}Z")
MARKERS = ("report.json", "run.json", "recipe.py", "before.json", "events.jsonl", "provenance.json")


def scenario_from_dir(name):
    match = RUN_DIR.match(name)
    return match.group("scenario") if match else name


def assess(run, criteria):
    test = run.report_test()
    scenario = (test or {}).get("id") or scenario_from_dir(run.path.name)
    recorded = (test or {}).get("status") if (test or {}).get("status") in STATUSES else None
    checks = common_checks(run, criteria, scenario, recorded)
    if recorded == "PASS":
        checks += scenario_checks(run, scenario, criteria["scenarios"].get(scenario, {}))
    required = [c for c in checks if c.severity == "required"]
    if any(c.outcome == "FAIL" for c in required):
        verification = "CONTRADICTED" if recorded == "PASS" else "INTEGRITY_ISSUES"
    elif any(c.outcome == "MISSING" for c in required):
        verification = "UNVERIFIED"
    else:
        verification = "VERIFIED"
    reasons = [f"{c.name}: {c.detail or c.outcome}" for c in required if c.outcome != "OK"]
    if recorded is None:
        effective, reasons = "INDETERMINATE", ["no readable recorded verdict"] + reasons
    elif recorded == "PASS":
        effective = "PASS" if verification == "VERIFIED" else "INDETERMINATE"
    else:
        effective = recorded                       # historical negatives and inconclusive results are kept as recorded
    return {"scenario": scenario, "recorded": recorded, "effective": effective, "verification": verification,
            "reasons": reasons, "checks": [c.as_dict() for c in checks]}


def context_of(run):
    device = ((run.json("before.json") or {}).get("device") or {})
    boards = ((run.json("manifest.json") or {}).get("boards") or [{}])
    transport = (run.json("transport.json") or {}).get("declared_by_operator") or (boards[0].get("transports") or ["?"])[0]
    return f"{device.get('chip', '?')}/{transport}"


def fingerprint(run, scenario):
    ops, events, rows = run.operations() or [], run.events() or [], run.jsonl("requests.jsonl") or []
    commands = [o["command"] for o in ops]                  # order AND repetitions are behaviour
    refused = sorted({("primary" if r["client"] == "primary" else "other", r["status"], r.get("code")) for r in rows if r.get("ok") is None and r.get("status")}, key=str)
    grants = ["+" if e["seconds"] > 0 else "0" for e in events if e.get("kind") == "grant"]
    before, after = run.json("before.json") or {}, run.json("after.json") or {}
    lines = evidence_lines(run) or []
    patterns = sorted({m for line in lines for m in re.findall(r"pattern sha256=([0-9a-f]{64})", line)})
    reads = sorted({m for line in lines for m in re.findall(r"two reads equal, sha256=([0-9a-f]{64})", line)})
    provenance = run.json("provenance.json") or {}
    return {
        "behaviour": {"scenario": scenario, "commands": commands, "refusals": [list(r) for r in refused], "grant_pattern": grants,
                      "windows": [w.get("event") for w in run.jsonl("windows.jsonl") or []],
                      "submissions": sorted((run.json("cleanup.json") or {}).get("submissions", {}).values()),
                      "epoch_changed": before.get("epoch") != after.get("epoch"),
                      "generation_changed": (before.get("device") or {}).get("generation") != (after.get("device") or {}).get("generation"),
                      "pattern_sha256": patterns},
        "card_specific": {"chip": (before.get("device") or {}).get("chip"), "flash_size": (before.get("device") or {}).get("flash_size"),
                          "read_sha256": reads,
                          "region": [(run.json("manifest.json") or {}).get("write_regions")]},
        "environment": {"recipe_git_sha": provenance.get("git_sha"), "server_sha256": (before.get("server_observed") or {}).get("server_sha256"),
                        "support_sha256": canonical_sha(provenance.get("support_sha256") or {})}}


def diff(a, b, prefix=""):
    out = []
    for key in sorted(set(a) | set(b)):
        if a.get(key) != b.get(key):
            out.append(f"{prefix}{key}: {a.get(key)!r} != {b.get(key)!r}")
    return out


# ---- campaign-level analysis --------------------------------------------------------------------------------------------
def parse_root(text):
    head, _, path = text.partition("=")
    label, _, role = head.partition(":")
    if not path or not label or role not in ("historic", "candidate", ""):
        raise SystemExit(f"--root expects LABEL:historic|candidate=PATH, got {text!r}")
    return label, role or "candidate", Path(path)


def discover(root):
    """Every attempt: a sub-directory named like a run (<scenario>-<UTC stamp>) or holding any run marker.

    An attempt interrupted before report.json exists must stay visible (it is then INDETERMINATE); nothing is opened for writing."""
    return sorted(p for p in root.iterdir() if p.is_dir() and (RUN_DIR.match(p.name) or any((p / m).is_file() for m in MARKERS)))


def analyse(roots, criteria):
    runs = []
    for label, role, root in roots:
        for path in sorted({p for p in discover(root)}):
            run = Run(path)
            row = assess(run, criteria)
            row.update({"root": label, "role": role, "dir": path.name, "context": context_of(run),
                        "report_sha256": sha256_file(path / "report.json") if (path / "report.json").is_file() else None,
                        "recipe_git_sha": (run.json("provenance.json") or {}).get("git_sha"),
                        "fingerprint": fingerprint(run, row["scenario"])})
            runs.append(row)
    return runs


def matrix(runs, criteria):
    contexts = sorted({r["context"] for r in runs})
    rows = []
    for req in criteria["requirements"]:
        entry = {"id": req["id"], "text": req["text"], "scenarios": req["scenarios"], "contexts": {}}
        for ctx in contexts:
            per, functional, repeatable = {}, True, True
            for scenario in req["scenarios"]:
                mine = [r for r in runs if r["context"] == ctx and r["scenario"] == scenario]
                counts = {s: sum(1 for r in mine if r["effective"] == s) for s in STATUSES}
                passes = [r for r in mine if r["effective"] == "PASS"]
                # repeatability counts EVERY candidate attempt (also unattributed ones: no report, unreadable context): one failure breaks the batch
                attempts = [r for r in runs if r["role"] == "candidate" and r["scenario"] == scenario and (r["context"] == ctx or r["context"].startswith("?"))]
                cand = [r for r in attempts if r["effective"] == "PASS"]
                stable = bool(cand) and len(cand) == len(attempts) and all(
                    not diff(r["fingerprint"]["behaviour"], cand[0]["fingerprint"]["behaviour"]) and
                    r["fingerprint"]["environment"] == cand[0]["fingerprint"]["environment"] for r in cand)
                per[scenario] = {"effective": counts, "runs": [r["dir"] for r in mine],
                                 "evidence": [f"{r['root']}/{r['dir']}/report.json sha256={r['report_sha256']}" for r in passes],
                                 "repeatable_runs": len(cand), "candidate_attempts": len(attempts), "stable": stable}
                functional &= bool(passes)
                repeatable &= len(cand) >= criteria["repeat_min"] and len(cand) == len(attempts) and stable
            if any(per[s]["runs"] for s in per):
                entry["contexts"][ctx] = {"scenarios": per, "functional": "DEMONSTRATED" if functional else "NOT_DEMONSTRATED",
                                          "repeatable": "DEMONSTRATED" if repeatable else "NOT_DEMONSTRATED"}
        rows.append(entry)
    return rows


def repeatability(runs):
    out, groups = [], {}
    for r in runs:
        if r["role"] == "candidate":
            groups.setdefault((r["context"], r["scenario"]), []).append(r)
    for (ctx, scenario), group in sorted(groups.items()):
        passing = [g for g in group if g["effective"] == "PASS"]
        failing = [f"{g['dir']}: {g['effective']}" for g in group if g["effective"] != "PASS"]
        base = passing[0] if passing else group[0]
        drift = [f"attempt not PASS - {f}" for f in failing]
        for other in passing[1:]:
            drift += [f"{other['dir']}: {d}" for d in diff(base["fingerprint"]["behaviour"], other["fingerprint"]["behaviour"], "behaviour.")]
            drift += [f"{other['dir']}: {d}" for d in diff(base["fingerprint"]["environment"], other["fingerprint"]["environment"], "environment.")]
        out.append({"context": ctx, "scenario": scenario, "runs": [g["dir"] for g in group], "attempts": len(group), "passes": len(passing),
                    "stable": not drift, "drift": drift})
    return out


def inter_card(runs):
    by = {}
    for r in runs:
        if r["role"] == "candidate" and r["effective"] == "PASS":
            by.setdefault(r["scenario"], {}).setdefault(r["context"], r)
    out = []
    for scenario, per in sorted(by.items()):
        names = sorted(per)
        for i, first in enumerate(names):
            for second in names[i + 1:]:
                out.append({"scenario": scenario, "between": [first, second],
                            "behaviour_differences": diff(per[first]["fingerprint"]["behaviour"], per[second]["fingerprint"]["behaviour"]),
                            "card_specific_ignored": ["chip", "flash_size", "read_sha256", "region"]})
    return out


def render_markdown(result):
    lines = ["# Qualification comparison report", "",
             f"Tool v{result['tool']['version']} (sha256 `{result['tool']['sha256'][:16]}`) - criteria sha256 `{result['criteria_sha256'][:16]}`", ""]
    if result["simulated"]:
        lines += ["> **SIMULATED EVIDENCE** - produced from synthetic fixtures; it shows the report format, it qualifies nothing.", ""]
    lines += ["## Runs", "", "| root | role | run | scenario | context | recorded | effective | verification |", "|---|---|---|---|---|---|---|---|"]
    for r in result["runs"]:
        lines.append(f"| {r['root']} | {r['role']} | {r['dir']} | {r['scenario']} | {r['context']} | {r['recorded']} | **{r['effective']}** | {r['verification']} |")
    lines += ["", "## Requirement matrix", ""]
    for req in result["matrix"]:
        lines.append(f"### {req['id']} - {req['text']}")
        for ctx, data in req["contexts"].items():
            lines.append(f"- `{ctx}`: functional **{data['functional']}**, repeatable **{data['repeatable']}**")
            for scenario, s in data["scenarios"].items():
                counts = ", ".join(f"{k}={v}" for k, v in s["effective"].items() if v)
                lines.append(f"  - {scenario}: {counts or 'no run'}; candidate PASS runs {s['repeatable_runs']}; stable {s['stable']}")
        lines.append("")
    lines += ["## Repeatability (candidate passes only)", ""]
    for g in result["repeatability"]:
        lines.append(f"- `{g['context']}` {g['scenario']}: {len(g['runs'])} run(s), stable **{g['stable']}**" + (f"; drift: {g['drift']}" if g["drift"] else ""))
    lines += ["", "## Inter-card behaviour", ""]
    for g in result["inter_card"]:
        lines.append(f"- {g['scenario']} {g['between']}: " + ("same behaviour" if not g["behaviour_differences"] else f"differences {g['behaviour_differences']}"))
    problems = [r for r in result["runs"] if r["reasons"]]
    lines += ["", "## Evidence problems (never hidden)", ""]
    lines += [f"- {r['dir']} ({r['recorded']} -> {r['effective']}, {r['verification']}): " + "; ".join(r["reasons"]) for r in problems] or ["- none"]
    lines += ["", "## Not covered", ""] + [f"- **{n['level']}**: {n['text']}" for n in result["not_covered"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--criteria", required=True)
    parser.add_argument("--criteria-sha256", help="refuse to run if the criteria file differs (pin it before the campaign)")
    parser.add_argument("--root", action="append", required=True, help="LABEL:historic|candidate=PATH of an evidence directory")
    parser.add_argument("--output", required=True, help="directory outside every evidence root")
    parser.add_argument("--simulated", action="store_true", help="mark the report as produced from synthetic fixtures")
    parser.add_argument("--strict", action="store_true", help="exit 1 if evidence integrity is broken (recorded PASS not verified, INTEGRITY_ISSUES); "
                        "exit 2 if integrity holds but a candidate attempt is not an effective PASS (qualification not met)")
    args = parser.parse_args(argv)
    criteria_path = Path(args.criteria)
    criteria_sha = sha256_file(criteria_path)
    if args.criteria_sha256 and args.criteria_sha256 != criteria_sha:
        raise SystemExit(f"criteria changed: pinned {args.criteria_sha256[:12]} != actual {criteria_sha[:12]}; refusing to compare")
    criteria = json.loads(criteria_path.read_text())
    roots = [parse_root(r) for r in args.root]
    output = Path(args.output).resolve()
    for _, _, root in roots:
        if output == root.resolve() or root.resolve() in output.parents:
            raise SystemExit("the output directory must not be inside an evidence root: evidence is never modified")
    runs = analyse(roots, criteria)
    result = {"tool": {"version": TOOL_VERSION, "sha256": sha256_file(__file__)}, "criteria_sha256": criteria_sha, "simulated": args.simulated,
              "roots": [{"label": label, "role": role, "path": str(root)} for label, role, root in roots],
              "runs": runs, "matrix": matrix(runs, criteria), "repeatability": repeatability(runs), "inter_card": inter_card(runs),
              "not_covered": criteria["not_covered"]}
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    (output / "comparison.json").write_text(json.dumps(result, indent=2, sort_keys=True))
    (output / "comparison.md").write_text(render_markdown(result))
    print(f"{len(runs)} runs analysed -> {output}/comparison.json, comparison.md")
    bad = [r for r in runs if r["recorded"] == "PASS" and r["verification"] != "VERIFIED"]
    if bad:
        print(f"WARNING: {len(bad)} recorded PASS could not be verified (reported INDETERMINATE)", file=sys.stderr)
    unmet = [r for r in runs if r["role"] == "candidate" and r["effective"] != "PASS"]
    if unmet:
        print(f"NOTE: {len(unmet)} candidate attempt(s) are not an effective PASS: " + ", ".join(f"{r['dir']}={r['effective']}" for r in unmet), file=sys.stderr)
    if args.strict:
        if bad or any(r["verification"] == "INTEGRITY_ISSUES" for r in runs):
            return 1
        if unmet:
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
