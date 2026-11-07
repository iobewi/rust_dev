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

TOOL_VERSION = "3"
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
                        row["_board"] = path.parent.name
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
    elif expected == "generation_any":          # the card may return with a new generation (USB re-enumeration), the server epoch never changes
        bad = [n for n, c in (("epoch changed", epoch_changed),) if c]
    elif expected == "generation_changed":      # an interruption of the agent / USB link: new generation, same server
        bad = [n for n, c in (("epoch changed", epoch_changed), ("generation did not change", not gen_changed)) if c]
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
    checks += driven_checks(run, scenario, params, lines, ops, grants)
    return checks


def driven_checks(run, scenario, params, lines, ops, grants):
    """Checks selected by the criteria file (criteria v2 scenarios). A criteria version without these keys adds nothing here."""
    checks = []

    def add(name, ok, detail="", missing=False):
        checks.append(Check(name, "MISSING" if missing else ("OK" if ok else "FAIL"), detail))
    manifest = run.json("manifest.json")
    if params.get("context"):
        boards = (manifest or {}).get("boards") or [{}]
        got = (boards[0].get("chip"), (boards[0].get("transports") or [None])[0])
        want = (params["context"]["chip"], params["context"]["transport"])
        add("context_matches", got == want, f"the manifest declares {got}, the scenario requires {want}", manifest is None)
    if params.get("transport_equals"):
        transport = run.json("transport.json") or {}
        add("transport_is_native_serial_jtag", transport.get("declared_by_operator") == params["transport_equals"] == transport.get("inferred_by_agent"),
            f"declared {transport.get('declared_by_operator')!r} and inferred {transport.get('inferred_by_agent')!r} must both be {params['transport_equals']!r}", not transport)
    if params.get("needs_transport"):
        transport = run.json("transport.json")
        ok = isinstance(transport, dict) and {"declared_by_operator", "inferred_by_agent", "provenance_source"} <= set(transport)
        add("transport_declared_and_inferred", ok, "declaration and agent inference must both be recorded", transport is None)
    for command, count in (params.get("exact_commands") or {}).items():
        seen = None if ops is None else sum(1 for o in ops if o["command"] == command)
        add(f"exactly_{count}_{command}", seen == count, f"{seen} {command} operation(s), expected {count}", ops is None)
    for item in params.get("evidence_patterns", []):
        checks.append(Check(item["name"], "OK" if has_evidence(lines, item["pattern"]) else "MISSING", item["detail"]))
    if params.get("no_interrupted_window"):
        add("no_continuity_break", not run.exists("windows.jsonl"), "a proof window was interrupted: this is not a plain PASS")
    if params.get("no_grant_events"):
        add("no_grant_events", grants == [], f"grant events observed: {[s for _, s in grants or []]}", grants is None)
    if params.get("grant_timeline") == "natural_expiry":
        add("natural_expiry", grants is not None and len(grants) == 1 and grants[0][1] > 0,
            f"grant events: {[s for _, s in grants or []]} (expiry means one positive event and no zero)", grants is None)
    for command, shape in (params.get("submitted_args") or {}).items():
        for op in [o for o in ops or [] if o["command"] == command]:
            sub = submission(run, op)
            if sub is None:
                add(f"submitted_args_{command}", False, f"no recorded submission for {command} operation {op['id']}", True)
                continue
            args = sub.get("args")
            ok = isinstance(args, dict) and set(args) == set(shape) and all(v == "*" or args.get(k) == v for k, v in shape.items())
            add(f"submitted_args_{command}", ok, f"{command} was submitted with {sorted(args) if isinstance(args, dict) else args!r}, the contract allows exactly {sorted(shape)}")
        if ops is None:
            add(f"submitted_args_{command}", False, "operations unreadable", True)
    if params.get("hold_ms"):
        low, high = params["hold_ms"]
        requests = run.jsonl("bootloader-requests.jsonl")
        boots = [o for o in ops or [] if o["command"] == "boot.download"]
        sub = submission(run, boots[0]) if len(boots) == 1 else None
        executed = ((sub or {}).get("args") or {}).get("hold_ms")
        ok = len(boots) == 1 and type(executed) is int and low <= executed <= high
        add("hold_ms_bounded", ok, f"the EXECUTED boot.download must carry one integer hold_ms in {low}..{high}, got {executed!r}", sub is None)
        intent = requests[0].get("hold_ms") if requests and len(requests) == 1 else None
        add("hold_ms_matches_request", sub is not None and executed == intent, f"executed hold_ms {executed!r} != recipe request {intent!r}", sub is None or requests is None)
    if params.get("uart_match"):
        checks.extend(uart_match_checks(run, params["uart_match"], ops))
    if params.get("console_return"):
        checks.extend(console_return_checks(run, params["console_return"], ops))
    if params.get("grant_revoked_locally"):
        add("grant_revoked_locally", bool(grants) and grants[-1][1] == 0 and any(s > 0 for _, s in grants),
            "no positive grant followed by a zero (revocation)", grants is None)
    if "min_regions" in params:
        regions = (manifest or {}).get("write_regions") or []
        n = len(regions)
        add("region_count", params["min_regions"] <= n <= params["max_regions"], f"{n} approved region(s)", manifest is None)
        if params.get("flash_write_count") == "1_plus_regions" and ops is not None:
            seen = sum(1 for o in ops if o["command"] == "flash.write")
            add("flash_write_count", seen == 1 + n, f"{seen} flash.write operation(s): expected one multi-image write plus one restoration per region ({1 + n})")
        if params.get("r4_correlation"):
            checks.extend(r4_correlation_checks(run, regions, ops))
        problems = []
        for i, region in enumerate(regions):
            a, b = run.json(f"board-0/ref-{i}-a.json"), run.json(f"board-0/ref-{i}-b.json")
            if not a or not b:
                problems.append(f"region {i}: ref sidecars unreadable")
            elif not (a.get("sha256") == b.get("sha256") and a.get("complete") and b.get("complete")
                      and (a.get("address"), a.get("size")) == (region["address"], region["size"])):
                problems.append(f"region {i}: backups differ, are incomplete or do not match the manifest region")
        if regions:
            add("backups_equal_per_region", not problems, "; ".join(problems), any("unreadable" in x for x in problems))
    checks.extend(raw_checks(run, scenario, params, ops, lines))
    if params.get("restoration_prefix"):
        test = run.report_test()
        checks.append(Check("restored_and_compared", "OK" if str((test or {}).get("restoration", "")).startswith(params["restoration_prefix"]) else "MISSING",
                            "restoration not proven"))
    return checks



# ---- raw-execution correlation (criteria v2) ----------------------------------------------------------------------------
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def submission(run, op):
    """The arguments the client actually submitted for an operation (board-N/commands.jsonl, written BEFORE the POST)."""
    seq, board = op.get("submit_seq"), op.get("_board")
    rows = run.jsonl(f"{board}/commands.jsonl") if board else None
    if rows is None or type(seq) is not int:
        return None
    found = [r for r in rows if r.get("seq") == seq]
    return found[0] if len(found) == 1 and found[0].get("command") == op.get("command") else None


def uart_match_checks(run, spec, ops):
    """Ties the console/ROM observation to the operation and to the cursor taken BEFORE its submission (no UART content is stored)."""
    out = []

    def add(name, ok, detail, missing=False):
        out.append(Check(name, "MISSING" if missing else ("OK" if ok else "FAIL"), detail))
    targets = [o for o in ops or [] if o["command"] == spec["command"]]
    if len(targets) != 1:
        add("uart_match_correlated", False, f"{len(targets)} {spec['command']} operation(s): cannot correlate", ops is None or not targets)
        return out
    op = targets[0]
    board = op.get("_board")
    matches, cursors = run.jsonl(f"{board}/uart-matches.jsonl"), run.jsonl(f"{board}/cursors.jsonl")
    requests, before = run.jsonl(spec["requests"]), run.json("before.json") or {}
    sub = submission(run, op)
    if matches is None or cursors is None or requests is None or sub is None:
        add("uart_match_correlated", False, "uart-matches.jsonl, cursors.jsonl, the requests file or the submission is absent", True)
        return out
    if len(matches) != 1 or len(requests) != 1:
        add("uart_match_correlated", False, f"{len(matches)} UART match record(s) and {len(requests)} request record(s): exactly one of each expected")
        return out
    m, req = matches[0], requests[0]
    cur = [c for c in cursors if c.get("index") == m.get("cursor_index")]
    problems = []
    if len(cur) != 1:
        problems.append("the matching cursor has no record")
    else:
        c = cur[0]
        if type(c.get("submitted")) is not int or type(op.get("submit_seq")) is not int or not c["submitted"] <= op["submit_seq"]:
            problems.append("the cursor was not taken before the command was submitted")
        if c.get("after") != m.get("cursor_after") or c.get("generation") != op.get("generation") or m.get("generation") != op.get("generation"):
            problems.append("cursor, match and operation disagree on generation or position")
    if not (type(m.get("matched_seq")) is int and type(m.get("cursor_after")) is int and m["matched_seq"] > m["cursor_after"]):
        problems.append("the matching UART event does not follow the cursor")
    if m.get("epoch") != before.get("epoch"):
        problems.append("the match belongs to another epoch")
    if m.get("operation") != op["id"]:
        problems.append(f"the match was recorded after another operation ({m.get('operation')!r}), not {op['id']}")
    pinned = spec.get("pattern_sha256")
    if not (isinstance(m.get("pattern_sha256"), str) and HEX64.match(m["pattern_sha256"]) and m["pattern_sha256"] == req.get("marker_sha256")):
        problems.append("the matched pattern differs from the marker the recipe declared")
    if pinned and m.get("pattern_sha256") != pinned:
        problems.append("the matched pattern is not the one pinned by the criteria")
    if not (type(m.get("pattern_len")) is int and m["pattern_len"] >= spec.get("min_pattern_len", 4)):
        problems.append("the pattern is too short to be a marker")
    if op.get("state") != "completed":
        problems.append(f"the operation is {op.get('state')!r}, not completed")
    add("uart_match_correlated", not problems, "; ".join(problems))
    return out


def console_return_checks(run, spec, ops):
    """The 'console opened' transport event must follow a cursor taken before the read was submitted, and be recorded after that read."""
    targets = [o for o in ops or [] if o["command"] == spec["command"]]
    if len(targets) != 1:
        return [Check("console_return_correlated", "MISSING", f"{len(targets)} {spec['command']} operation(s): cannot correlate")]
    op = targets[0]
    board = op.get("_board")
    events, cursors = run.jsonl("console-events.jsonl"), run.jsonl(f"{board}/cursors.jsonl")
    if events is None or cursors is None:
        return [Check("console_return_correlated", "MISSING", "console-events.jsonl or cursors.jsonl absent")]
    opened = [e for e in events if "console opened" in str(e.get("detail", "")) and e.get("kind") == "transport"]
    problems = []
    if len(opened) != 1:
        problems.append(f"{len(opened)} 'console opened' transport event(s), exactly one expected")
    else:
        e = opened[0]
        cur = [c for c in cursors if c.get("index") == e.get("cursor_index")]
        if len(cur) != 1 or type(cur[0].get("submitted")) is not int or type(op.get("submit_seq")) is not int or not cur[0]["submitted"] <= op["submit_seq"]:
            problems.append("the cursor was not taken before the read was submitted")
        elif cur[0].get("after") != e.get("cursor_after_start") or cur[0].get("generation") != op.get("generation"):
            problems.append("cursor and operation disagree on position or generation")
        if not (type(e.get("seq")) is int and type(e.get("cursor_after_start")) is int and e["seq"] > e["cursor_after_start"]):
            problems.append("the transport event does not follow the cursor")
        if e.get("operation") != op["id"]:
            problems.append("the event was gathered after another operation")
    if op.get("state") != "completed":
        problems.append(f"the read is {op.get('state')!r}, not completed")
    return [Check("console_return_correlated", "FAIL" if problems else "OK", "; ".join(problems))]


def read_observations(run, ops):
    """Every flash read sidecar: (op index, address, size, sha256), ordered by the operation sequence."""
    order = {o["id"]: i for i, o in enumerate(ops)}
    rows = []
    for sidecar in sorted(run.path.glob("board-*/flash-*.json")):
        meta = run.json(str(sidecar.relative_to(run.path)))
        if isinstance(meta, dict) and meta.get("operation") in order and HEX64.match(str(meta.get("sha256", ""))):
            rows.append({"idx": order[meta["operation"]], "address": meta.get("address"), "size": meta.get("size"), "sha256": meta["sha256"],
                         "blob": sidecar.with_suffix(".bin")})
    return sorted(rows, key=lambda r: r["idx"])


def overlaps(a, size_a, b, size_b):
    return a < b + size_b and b < a + size_a


def r4_correlation_checks(run, regions, ops):
    """Independent R4 evidence: what was submitted, what was read back, and the control windows - from raw files, not from report text."""
    out = []

    def add(name, ok, detail, missing=False):
        out.append(Check(name, "MISSING" if missing else ("OK" if ok else "FAIL"), detail))
    if ops is None:
        add("r4_images_match_manifest", False, "operations unreadable", True)
        return out
    writes = [(i, o) for i, o in enumerate(ops) if o["command"] == "flash.write"]
    subs = [submission(run, o) for _, o in writes]
    if not writes or any(s is None for s in subs):
        add("r4_images_match_manifest", False, "flash.write submissions are not recorded (commands.jsonl)", True)
        return out
    refs = []
    for i in range(len(regions)):
        a = run.json(f"board-0/ref-{i}-a.json")
        refs.append(a if isinstance(a, dict) else None)
    if any(r is None for r in refs):
        add("r4_images_match_manifest", False, "region reference sidecars unreadable", True)
        return out
    first = (subs[0].get("args") or {})
    images = first.get("images") if isinstance(first, dict) else None
    problems = []
    if not isinstance(images, list) or not images or not all(isinstance(x, dict) for x in images):
        problems.append("the first flash.write carries no image list")
        images = []
    got = sorted((x.get("address"), x.get("sha256")) for x in images)
    if sorted(x.get("address") for x in images) != sorted(r["address"] for r in regions) or len(images) != len(regions):
        problems.append(f"the first flash.write programmed {sorted(x.get('address') for x in images)}, the manifest approves {sorted(r['address'] for r in regions)}")
    if not all(isinstance(x.get("sha256"), str) and HEX64.match(x["sha256"]) for x in images) or len({x.get("sha256") for x in images}) != len(images):
        problems.append("image hashes are missing, malformed or not pairwise distinct")
    if first.get("return_console") is not False:
        problems.append("the multi-image write did not set return_console=false")
    for region, ref in zip(regions, refs):
        mine = [x for x in images if x.get("address") == region["address"]]
        if mine and mine[0].get("sha256") == ref.get("sha256"):
            problems.append(f"image at {region['address']:#x} equals the region's reference: nothing would be proven")
    add("r4_images_match_manifest", not problems, "; ".join(problems))
    reads = read_observations(run, ops)
    w0 = writes[0][0]
    w1 = writes[1][0] if len(writes) > 1 else None
    # (b) every region was read back after the multi-image write, before any restoration, and equals ITS image
    rb = []
    for region in regions:
        want = next((x.get("sha256") for x in images if x.get("address") == region["address"]), None)
        seen = [r for r in reads if r["address"] == region["address"] and r["size"] == region["size"] and r["idx"] > w0 and (w1 is None or r["idx"] < w1)]
        if not seen or any(r["sha256"] != want for r in seen):
            rb.append(f"region {region['address']:#x}: {'no readback after the write' if not seen else 'readback differs from the submitted image'}")
    add("r4_readbacks_equal_submitted_images", not rb, "; ".join(rb))
    # (c) restoration: one write per region, carrying the backup's own hash, then a comparing read equal to the backup
    rs = []
    restores = writes[1:]
    restore_subs = subs[1:]
    covered = []
    for (idx, op), sub in zip(restores, restore_subs):
        imgs = ((sub.get("args") or {}).get("images")) or []
        if len(imgs) != 1:
            rs.append(f"restoration write {op['id']} carries {len(imgs)} image(s)")
            continue
        covered.append((imgs[0].get("address"), imgs[0].get("sha256"), idx))
    for region, ref in zip(regions, refs):
        mine = [c for c in covered if c[0] == region["address"]]
        if len(mine) != 1 or mine[0][1] != ref.get("sha256"):
            rs.append(f"region {region['address']:#x}: restoration write missing, duplicated or not the backup's hash")
            continue
        later = [r for r in reads if r["address"] == region["address"] and r["size"] == region["size"] and r["idx"] > mine[0][2]]
        if not later or later[0]["sha256"] != ref.get("sha256"):
            rs.append(f"region {region['address']:#x}: no comparing read equal to the backup after its restoration")
    if len(restores) != len(regions):
        rs.append(f"{len(restores)} restoration write(s) for {len(regions)} region(s)")
    add("r4_restoration_matches_backups", not rs, "; ".join(rs))
    # (d) control windows: declared in r4-windows.json, hashes from the sidecars of the reads themselves
    declared = run.json("r4-windows.json")
    if not isinstance(declared, list) or not declared:
        add("r4_control_windows_unchanged", False, "r4-windows.json absent or empty", True)
        return out
    last_write = writes[-1][0]
    wp = []
    informative = False
    for win in declared:
        a, n = win.get("address"), win.get("size")
        if type(a) is not int or type(n) is not int or n <= 0:
            wp.append(f"malformed window {win!r}")
            continue
        if any(overlaps(a, n, r["address"], r["size"]) for r in regions):
            wp.append(f"window {a:#x} overlaps an approved region: it cannot be an oracle")
        seen = [r for r in reads if r["address"] == a and r["size"] == n]
        pre = [r for r in seen if r["idx"] < w0]
        mid = [r for r in seen if r["idx"] > w0 and (w1 is None or r["idx"] < w1)]
        post = [r for r in seen if r["idx"] > last_write]
        if len(pre) < 2 or not mid or not post:
            wp.append(f"window {a:#x}: reads before/after-write/after-restore = {len(pre)}/{len(mid)}/{len(post)} (needs >=2/1/1)")
            continue
        if len({r["sha256"] for r in pre + mid + post}) != 1:
            wp.append(f"window {a:#x}: its digest changed between the reads")
        try:
            informative = informative or any(b != 0xFF for b in pre[0]["blob"].read_bytes())
        except OSError:
            wp.append(f"window {a:#x}: blob unreadable")
    if not informative and not wp:
        wp.append("every window is all 0xFF: an erase could not be seen")
    add("r4_control_windows_unchanged", not wp, "; ".join(wp))
    return out



# ---- criteria v3 checks: raw files written by the recipes and the client, cross-checked with the server's own records -------------------
def raw_checks(run, scenario, params, ops, lines):
    out = []

    def add(name, ok, detail="", missing=False):
        out.append(Check(name, "MISSING" if missing else ("OK" if ok else "FAIL"), detail))
    for command, states in (params.get("final_states") or {}).items():
        mine = [o for o in ops or [] if o["command"] == command]
        add(f"final_state_{command}", ops is not None and bool(mine) and all(o["state"] in states for o in mine),
            f"{command} ended {[o['state'] for o in mine]}, allowed {states}", ops is None)
    if params.get("exact_submissions"):
        rows = [r for p in sorted(run.path.glob("board-*/commands.jsonl")) for r in (run.jsonl(str(p.relative_to(run.path))) or [])]
        for command, count in params["exact_submissions"].items():
            seen = sum(1 for r in rows if r.get("command") == command)
            add(f"submitted_{count}_{command}", seen == count, f"{seen} {command} submission(s) recorded BEFORE their POST, expected {count} (a replay adds one)", not rows)
    for want in params.get("required_refusals", []):
        rows = [r for r in (refusals(run) or []) if (r["status"], r.get("code")) == (want["status"], want["code"])]
        add(f"refused_{want['status']}_{want['code']}", len(rows) >= want["min"], f"{len(rows)} refusal(s) {want['status']} {want['code']}, expected >= {want['min']}", refusals(run) is None)
    for name in ("trigger_observations", "reenumeration", "interruption", "expiry", "identities", "lan_observations", "capture"):
        if params.get(name):
            out.extend(globals()[f"{name}_checks"](run, params[name], ops))
    return out


def _check(out, name, problems, missing=False):
    out.append(Check(name, "MISSING" if missing else ("FAIL" if problems else "OK"), "; ".join(problems)))


def trigger_observations_checks(run, spec, ops):
    out, seen, events = [], run.json("trigger-observations.json"), run.events()
    if not isinstance(seen, dict) or events is None:
        _check(out, "trigger_observations", [], True)
        return out
    log = {(e["kind"], e["seq"]) for e in events}
    problems, by_name = [], {c.get("name"): c for c in seen.get("cases", [])}
    for case in spec["cases"]:
        got = by_name.get(case["name"])
        if got is None:
            problems.append(f"case {case['name']} absent")
            continue
        if (len(got["banners"]), len(got["triggers"])) != (case["banners"], case["triggers"]):
            problems.append(f"{case['name']}: {len(got['banners'])} banner(s) / {len(got['triggers'])} trigger event(s), expected {case['banners']} / {case['triggers']}")
        # every observed event must exist in the server's own event log (the recipe's list is corroborated, not trusted)
        problems += [f"{case['name']}: {kind} event seq {e['seq']} absent from the server log" for kind, key in (("uart", "banners"), ("trigger", "triggers"))
                     for e in got[key] if (kind, e["seq"]) not in log]
        if case["triggers"] and got["banners"] and got["triggers"] and not got["banners"][0]["seq"] < got["triggers"][0]["seq"]:
            problems.append(f"{case['name']}: the trigger event does not follow banner #1")
        if case.get("deferred_after_trigger") and len(got["banners"]) == 2 and not got["triggers"][0]["seq"] < got["banners"][1]["seq"]:
            problems.append(f"{case['name']}: the deferred banner does not follow the trigger event")
        if case.get("min_gap_factor") and len(got["banners"]) == 2:
            gap = (got["banners"][1]["at"] - got["banners"][0]["at"]) * 1000
            if gap < case["min_gap_factor"] * got["delay_ms"]:
                problems.append(f"{case['name']}: deferred reset after {gap:.0f} ms, before {case['min_gap_factor']} x {got['delay_ms']} ms")
    resets = sum(1 for o in ops or [] if o["command"] == "reset")
    if resets != spec["resets"]:
        problems.append(f"{resets} reset operation(s), expected {spec['resets']}")
    _check(out, "trigger_observations", problems)
    return out


def reenumeration_checks(run, spec, ops):
    out, seen = [], run.json("reenumeration.json")
    before, after = run.json("before.json"), run.json("after.json")
    windows = run.jsonl("windows.jsonl") if run.exists("windows.jsonl") else []
    if not isinstance(seen, dict) or not before or not after:
        _check(out, "reenumeration_consistent", [], True)
        return out
    changed = before["device"].get("generation") != after["device"].get("generation")
    interrupted = [w for w in windows or [] if w.get("event") == "interrupted"]
    problems = []
    if bool(seen.get("observed")) != changed:
        problems.append(f"reenumeration.json says observed={seen.get('observed')} but the generation {'changed' if changed else 'did not change'}")
    if bool(seen.get("observed")) != bool(interrupted):
        problems.append("a re-enumeration must close a proof window explicitly (and only then)")
    if seen.get("observed") and sum(1 for w in windows if w.get("event") == "opened") < 1:
        problems.append("no new proof window was opened after the re-enumeration")
    _check(out, "reenumeration_consistent", problems)
    return out


def interruption_checks(run, spec, ops):
    out, seen, server = [], run.json("interruption.json"), run.json("interrupted-operation.json")
    after = (run.json("after.json") or {}).get("device") or {}
    if not isinstance(seen, dict) or not isinstance(server, dict) or ops is None:
        _check(out, "interruption_consistent", [], True)
        return out
    reads = [o for o in ops if o["command"] == spec["command"]]
    fault, recovered = server.get("after_fault") or {}, server.get("after_recovery") or {}
    problems = []
    if len(reads) != 1:
        problems.append(f"{len(reads)} {spec['command']} operation(s), exactly one expected (no replay)")
    elif reads[0]["state"] == "completed" or reads[0]["id"] != fault.get("id"):
        problems.append(f"the client journal ends {reads[0]['state']!r} for {reads[0]['id']}; the server record is for {fault.get('id')}")
    if fault.get("state") not in spec["states"]:
        problems.append(f"the server reports state {fault.get('state')!r}, allowed {spec['states']}")
    if fault.get("state") != recovered.get("state") or fault.get("id") != recovered.get("id"):
        problems.append("the interrupted operation changed after the recovery (replay?)")
    if seen.get("state") != fault.get("state"):
        problems.append(f"the recipe summary says {seen.get('state')!r}, the server says {fault.get('state')!r}")
    if seen.get("submissions") != 1:
        problems.append(f"{seen.get('submissions')} submissions before the interruption was judged")
    if fault.get("state") == "indeterminate" and not seen.get("quarantined_after_reconnect"):
        problems.append("an indeterminate operation did not keep the card blocked after reconnection")
    if after.get("quarantined"):
        problems.append("the card is still quarantined at the end (no operator recovery observed)")
    if seen.get("reservation_after") is not None:
        # a lease may outlive a SERIAL failure only (agent still connected); it must be judged so, belong to a definitively failed operation and be gone at the end
        if seen.get("reservation_judgement") != "held_by_owner" or fault.get("state") != "failed":
            problems.append("a reservation survived the fault without the held_by_owner judgement on a definitively failed operation")
    if after.get("reservation") is not None:
        problems.append("a reservation remains at the end of the run")
    if fault.get("state") == "indeterminate" and not has_evidence(lines_of(run), r"block lifted only after the operator"):
        problems.append("no recovery-by-the-operator evidence for an indeterminate operation")
    _check(out, "interruption_consistent", problems)
    return out


def lines_of(run):
    return evidence_lines(run) or []


def expiry_checks(run, spec, ops):
    out, seen = [], run.json("expiry.json")
    if not isinstance(seen, dict):
        _check(out, "expiry_timing", [], True)
        return out
    ttl, elapsed = seen.get("ttl_s"), seen.get("device_free_after_s")
    ok = type(ttl) is int and isinstance(elapsed, (int, float)) and ttl - 1 <= elapsed <= ttl + 10 and ttl <= spec["max_ttl_s"]
    _check(out, "expiry_timing", [] if ok else [f"device free after {elapsed} s for a TTL of {ttl} s (allowed {ttl}-1..{ttl}+10, TTL <= {spec['max_ttl_s']})"])
    return out


def identities_checks(run, spec, ops):
    out, seen = [], run.json("identities.json")
    if not isinstance(seen, dict) or ops is None:
        _check(out, "two_boards", [], True)
        return out
    chip = ((run.json("before.json") or {}).get("device") or {}).get("chip")
    problems = [f"{label} card measured {item.get('chip')!r}, the declared card is {chip!r}" for label, item in seen.items() if item.get("chip") != chip]
    if len(seen) != 2:
        problems.append(f"{len(seen)} identities recorded, two expected")
    if len({o["_board"] for o in ops if o["command"] == "identify"}) < 2:
        problems.append("identify did not run through two distinct reservations")
    _check(out, "two_boards", problems)
    return out


def lan_observations_checks(run, spec, ops):
    out, seen = [], run.json("lan-observations.json")
    if not isinstance(seen, dict) or ops is None:
        _check(out, "lan_contract", [], True)
        return out
    cases, hits = {c["name"]: c for c in seen.get("cases", [])}, seen.get("target_hits", [])
    problems = []
    if set(cases) != {"ok", "redirect", "not_listed", "https", "credentials"}:
        problems.append(f"cases {sorted(cases)}")
    else:
        paths = [h["path"] for h in hits]
        if cases["ok"].get("result") != {"response": "opaque", "verified": False}:
            problems.append("the ok case is not an opaque, unverified response")
        if cases["redirect"].get("state") not in ("failed", "indeterminate"):
            problems.append("the redirect case did not fail")
        if sorted(paths) != ["/ok", "/redirect"] or "/after-redirect" in paths:
            problems.append(f"the target saw {sorted(paths)}")
        if any(h.get(k) for h in hits for k in ("cookie", "authorization", "referer", "origin")):
            problems.append("a request carried a Cookie, Authorization, Referer or Origin header")
        for name in ("not_listed", "https", "credentials"):
            if "refused" not in cases[name]:
                problems.append(f"{name} was not refused")
    if sum(1 for o in ops if o["command"] == "http.request") != 2:
        problems.append("exactly two http.request operations must have reached the agent (the other three are refused by the server)")
    _check(out, "lan_contract", problems)
    return out


def capture_checks(run, spec, ops):
    out, stats, index = [], run.json("capture-stats.json"), run.jsonl("capture-index.jsonl")
    if not isinstance(stats, dict) or index is None:
        _check(out, "capture_continuity", [], True)
        return out
    again = analyse_capture(index)                    # recomputed from the index, not from the recipe's summary
    problems = [f"{k}: summary {stats.get(k)} != recomputed {again[k]}" for k in ("events", "bytes", "silent_loss", "flagged_loss", "duplicates") if stats.get(k) != again[k]]
    if again["bytes"] < spec["min_bytes"]:
        problems.append(f"{again['bytes']} bytes, below the criterion {spec['min_bytes']}")
    if stats.get("seconds", 0) < spec["min_seconds"]:
        problems.append(f"{stats.get('seconds')} s window, below the criterion {spec['min_seconds']} s")
    if again["silent_loss"] or again["duplicates"] or stats.get("server_lost"):
        problems.append(f"silent_loss={again['silent_loss']} duplicates={again['duplicates']} server_lost={stats.get('server_lost')}")
    if again["flagged_loss"] > spec["max_flagged_loss"]:
        problems.append(f"{again['flagged_loss']} flagged losses > {spec['max_flagged_loss']}")
    problems += archive_problems(run, stats, index)
    if stats.get("resumes", 0) < 1 or len(stats.get("epochs", [])) != 1 or stats.get("disconnected_events") or stats.get("generations", 0) > 1:
        problems.append("no deliberate resume, or the epoch / generation / connection changed during the window")
    _check(out, "capture_continuity", problems)
    return out


def archive_problems(run, stats, index):
    """The archive must be the exact concatenation of the indexed chunks: size, whole-file SHA-256 and the SHA-256 of every chunk."""
    path = run.path / "uart-archive.bin"
    try:
        data = path.read_bytes()
    except OSError:
        return ["uart-archive.bin is missing or unreadable"]
    problems, expected = [], sum(r["bytes"] for r in index)
    if len(data) != expected:
        problems.append(f"archive is {len(data)} bytes, the index accounts for {expected} (truncated or extended)")
    if stats.get("archive_bytes") != len(data):
        problems.append(f"archive size {len(data)} != recorded {stats.get('archive_bytes')}")
    if stats.get("archive_sha256") != hashlib.sha256(data).hexdigest():
        problems.append("archive SHA-256 differs from the recorded one (corrupted)")
    offset, bad = 0, 0
    for row in index:
        chunk = data[offset:offset + row["bytes"]]
        offset += row["bytes"]
        bad += hashlib.sha256(chunk).hexdigest() != row.get("sha256")
    if bad:
        problems.append(f"{bad} indexed chunk(s) do not match their recorded SHA-256")
    return problems


def analyse_capture(rows):
    """Same continuity analysis as tools/sse_capture.analyse (kept here so that the comparator depends on nothing but its inputs)."""
    silent = flagged = duplicates = 0
    seen, previous = set(), {}
    for row in rows:
        duplicates += row["seq"] in seen
        seen.add(row["seq"])
        last = previous.get(row.get("generation"))
        gap = 0 if last is None else max(0, row["source_seq"] - last - 1)
        previous[row.get("generation")] = row["source_seq"]
        flagged += row["source_lost"]
        if last is not None and gap != row["source_lost"]:
            silent += abs(gap - row["source_lost"])
    return {"events": len(rows), "bytes": sum(r["bytes"] for r in rows), "silent_loss": silent, "flagged_loss": flagged, "duplicates": duplicates}


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
        if scenario not in criteria["scenarios"]:
            # No acceptance criteria => no verified PASS. Without this a PASS of a scenario the criteria version does not know would
            # be "verified" by the common checks alone.
            checks.append(Check("scenario_criteria_known", "MISSING", f"no acceptance criteria for scenario {scenario!r} in this criteria version"))
        else:
            checks += scenario_checks(run, scenario, criteria["scenarios"][scenario])
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
