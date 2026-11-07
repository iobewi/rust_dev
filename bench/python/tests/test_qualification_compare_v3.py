"""Criteria v3 and the raw-data checks of the comparator. SIMULATED/hand-made evidence only. v1 and v2 stay untouched; every v3 check must reject
the inconsistent raw files it exists for (a recipe's summary is never trusted over the server's own records)."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "tools"))
import qualification_compare as qc                                       # noqa: E402

V1, V2, V3 = (json.loads((BENCH / "tools" / n).read_text()) for n in ("qualification_criteria.json", "qualification_criteria_v2.json", "qualification_criteria_v3.json"))
NEW = ["NUSB-0", "NUSB-1", "NUSB-2", "T7", "R5-RELOAD", "R5-UNPLUG", "R5-RESTART", "G9", "M2", "H10", "U8"]


def test_v1_and_v2_are_untouched_and_v3_extends_v2_without_changing_any_entry():
    assert qc.sha256_file(BENCH / "tools" / "qualification_criteria.json") == "2dfc47e20ff7e4e2fe7d5f62a0ae69387e66ae190e8a28ff4b1d68b80760760c"
    assert qc.sha256_file(BENCH / "tools" / "qualification_criteria_v2.json") == "57fa046e54c46b408244c64d2514e6956a106c3fed26fee17b5720d1cae5a43e"
    assert V3["version"] == 3 and {k: V3["scenarios"][k] for k in V2["scenarios"]} == V2["scenarios"]
    assert V3["requirements"][:len(V2["requirements"])] == V2["requirements"] and V3["common"] == V2["common"] and V3["repeat_min"] == V2["repeat_min"]
    assert set(V3["scenarios"]) - set(V2["scenarios"]) == set(NEW)
    assert {s for r in V3["requirements"][len(V2["requirements"]):] for s in r["scenarios"]} == set(NEW)
    assert V3["not_covered"][:len(V2["not_covered"])] == V2["not_covered"]


def run_dir(tmp_path, files):
    for name, value in files.items():
        if name == "_archive":
            (tmp_path / "uart-archive.bin").write_bytes(value)
            continue
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in value) if name.endswith(".jsonl") else json.dumps(value))
    return qc.Run(tmp_path)


def outcome(out):
    return {c.name: c.outcome for c in out}


def ops(*items):
    return [{"id": f"op{i}", "command": c, "state": s, "_board": b, "submit_seq": i} for i, (c, s, b) in enumerate(items)]


# --- T7 ----------------------------------------------------------------------------------------------------------------
SPEC_T7 = V3["scenarios"]["T7"]["trigger_observations"]


def t7_files(**change):
    ev = lambda seq, at=0.0: {"seq": seq, "at": at}      # noqa: E731
    cases = [{"name": "fire_once", "delay_ms": 1500, "banners": [ev(10, 100.0), ev(14, 101.7)], "triggers": [ev(11, 100.1)]},
             {"name": "rearm_cancels", "delay_ms": 4000, "banners": [ev(20, 200.0)], "triggers": [ev(21, 200.1)]}]
    log = [{"seq": s, "kind": k} for k, s in (("uart", 10), ("trigger", 11), ("uart", 14), ("uart", 20), ("trigger", 21))]
    return {"trigger-observations.json": {"cases": change.get("cases", cases)}, "events.jsonl": log + change.get("extra_log", [])}


@pytest.mark.parametrize("label,mutate,ok", [
    ("consistent", lambda f: f, True),
    ("a third banner", lambda f: f["trigger-observations.json"]["cases"][0]["banners"].append({"seq": 15, "at": 103.0}), False),
    ("a second trigger event after the re-arm", lambda f: f["trigger-observations.json"]["cases"][1]["triggers"].append({"seq": 22, "at": 201.0}), False),
    ("the deferred reset came too early", lambda f: f["trigger-observations.json"]["cases"][0]["banners"][1].update(at=100.2), False),
    ("an observed banner absent from the server log", lambda f: f["events.jsonl"].pop(0), False),
    ("the trigger event precedes banner #1", lambda f: f["trigger-observations.json"]["cases"][0]["triggers"][0].update(seq=9), False),
])
def test_trigger_observations(tmp_path, label, mutate, ok):
    files = t7_files()
    mutate(files)
    if label.startswith("an observed banner"):
        files["events.jsonl"] = files["events.jsonl"]
    run = run_dir(tmp_path, files)
    out = qc.trigger_observations_checks(run, SPEC_T7, ops(("reset", "completed", "board-0"), ("reset", "completed", "board-0")))
    assert (outcome(out)["trigger_observations"] == "OK") is ok, (label, [c.detail for c in out])


def test_trigger_needs_exactly_the_expected_resets(tmp_path):
    out = qc.trigger_observations_checks(run_dir(tmp_path, t7_files()), SPEC_T7, ops(("reset", "completed", "board-0")))
    assert outcome(out)["trigger_observations"] == "FAIL"


# --- NUSB-2 re-enumeration ---------------------------------------------------------------------------------------------
def reenum_files(observed, changed, interrupted):
    gen = lambda g: {"device": {"generation": g}}                                  # noqa: E731
    windows = ([{"window": 0, "event": "interrupted"}, {"window": 1, "event": "opened"}] if interrupted else [])
    files = {"reenumeration.json": {"observed": observed}, "before.json": gen("g1"), "after.json": gen("g2" if changed else "g1")}
    if windows:
        files["windows.jsonl"] = windows
    return files


@pytest.mark.parametrize("observed,changed,interrupted,ok", [(False, False, False, True), (True, True, True, True), (True, False, True, False),
                                                              (False, True, False, False), (True, True, False, False), (False, False, True, False)])
def test_reenumeration_is_consistent_with_the_generation_and_the_windows(tmp_path, observed, changed, interrupted, ok):
    out = qc.reenumeration_checks(run_dir(tmp_path, reenum_files(observed, changed, interrupted)), {}, [])
    assert (outcome(out)["reenumeration_consistent"] == "OK") is ok


# --- R5 ------------------------------------------------------------------------------------------------------------------
SPEC_R5 = V3["scenarios"]["R5-RELOAD"]["interruption"]


def r5_files(state="indeterminate", quarantined=True, after_state=None, submissions=1, end_quarantine=False, evidence=True):
    keep = lambda s: {"id": "op0", "command": "flash.read", "state": s}            # noqa: E731
    files = {"interruption.json": {"state": state, "quarantined_after_reconnect": quarantined, "submissions": submissions},
             "interrupted-operation.json": {"after_fault": keep(state), "after_recovery": keep(after_state or state)},
             "after.json": {"device": {"quarantined": end_quarantine}},
             "report.json": {"tests": [{"id": "R5-RELOAD", "evidence": ["the card BLOCKED...", "block lifted only after the operator's local recovery gesture"] if evidence else []}]}}
    return files


@pytest.mark.parametrize("label,kwargs,operations,ok", [
    ("indeterminate, blocked, recovered", {}, ops(("flash.read", "running", "board-0")), True),
    ("failed explicitly", {"state": "failed", "quarantined": False, "evidence": False}, ops(("flash.read", "running", "board-0")), True),
    ("completed", {"state": "completed"}, ops(("flash.read", "completed", "board-0")), False),
    ("replayed: two reads", {}, ops(("flash.read", "running", "board-0"), ("flash.read", "running", "board-0")), False),
    ("two submissions", {"submissions": 2}, ops(("flash.read", "running", "board-0")), False),
    ("indeterminate but not blocked", {"quarantined": False}, ops(("flash.read", "running", "board-0")), False),
    ("state changed after recovery", {"after_state": "completed"}, ops(("flash.read", "running", "board-0")), False),
    ("still quarantined at the end", {"end_quarantine": True}, ops(("flash.read", "running", "board-0")), False),
    ("no operator recovery evidence", {"evidence": False}, ops(("flash.read", "running", "board-0")), False),
])
def test_interruption(tmp_path, label, kwargs, operations, ok):
    run = run_dir(tmp_path, r5_files(**kwargs))
    assert (outcome(qc.interruption_checks(run, SPEC_R5, operations))["interruption_consistent"] == "OK") is ok, label


# --- G9, M2, H10, U8 ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("ttl,elapsed,ok", [(8, 8.4, True), (8, 3.0, False), (8, 30.0, False), (600, 600.0, False)])
def test_expiry_timing(tmp_path, ttl, elapsed, ok):
    run = run_dir(tmp_path, {"expiry.json": {"ttl_s": ttl, "device_free_after_s": elapsed}})
    assert (outcome(qc.expiry_checks(run, V3["scenarios"]["G9"]["expiry"], []))["expiry_timing"] == "OK") is ok


@pytest.mark.parametrize("second_chip,boards,ok", [("esp32s3", ["board-0", "board-second", "board-0", "board-second"], True), ("esp32c3", ["board-0", "board-second"], False),
                                                    ("esp32s3", ["board-0", "board-0"], False)])
def test_two_boards(tmp_path, second_chip, boards, ok):
    run = run_dir(tmp_path, {"identities.json": {"first": {"chip": "esp32s3"}, "second": {"chip": second_chip}}, "before.json": {"device": {"chip": "esp32s3"}}})
    out = qc.identities_checks(run, {}, ops(*[("identify", "completed", b) for b in boards]))
    assert (outcome(out)["two_boards"] == "OK") is ok


def lan(**change):
    cases = [{"name": "ok", "result": {"response": "opaque", "verified": False}}, {"name": "redirect", "state": "failed"}, {"name": "not_listed", "refused": [403, "x"]},
             {"name": "https", "refused": [400, "x"]}, {"name": "credentials", "refused": [400, "x"]}]
    hits = [{"path": "/ok"}, {"path": "/redirect"}]
    return {"lan-observations.json": {"cases": change.get("cases", cases), "target_hits": change.get("hits", hits)}}


@pytest.mark.parametrize("label,change,count,ok", [
    ("consistent", {}, 2, True),
    ("redirect followed", {"hits": [{"path": "/ok"}, {"path": "/redirect"}, {"path": "/after-redirect"}]}, 2, False),
    ("a cookie was sent", {"hits": [{"path": "/ok", "cookie": True}, {"path": "/redirect"}]}, 2, False),
    ("ok case verified", {"cases": [{"name": "ok", "result": {"response": "opaque", "verified": True}}] + lan()["lan-observations.json"]["cases"][1:]}, 2, False),
    ("a refused case reached the agent", {}, 3, False),
])
def test_lan_contract(tmp_path, label, change, count, ok):
    out = qc.lan_observations_checks(run_dir(tmp_path, lan(**change)), {}, ops(*([("http.request", "completed", "board-0")] * count)))
    assert (outcome(out)["lan_contract"] == "OK") is ok, label


def _chunk(row):
    return bytes([row["seq"] % 251]) * row["bytes"]


def capture_files(rows=None, **stats):
    rows = [{**r, "sha256": hashlib.sha256(_chunk(r)).hexdigest()} for r in
            (rows or [{"seq": i, "source_seq": i, "source_lost": 0, "bytes": 2000, "generation": 1} for i in range(1, 61)])]
    archive = b"".join(_chunk(r) for r in rows)
    base = {**qc.analyse_capture(rows), "seconds": 40, "resumes": 3, "epochs": ["e"], "disconnected_events": 0, "generations": 1, "server_lost": 0,
            "archive_bytes": len(archive), "archive_sha256": hashlib.sha256(archive).hexdigest()}
    return {"capture-stats.json": {**base, **stats}, "capture-index.jsonl": rows, "_archive": archive}


SPEC_U8 = V3["scenarios"]["U8"]["capture"]


@pytest.mark.parametrize("label,files,ok", [
    ("consistent", capture_files(), True),
    ("too few bytes", capture_files(rows=[{"seq": i, "source_seq": i, "source_lost": 0, "bytes": 10, "generation": 1} for i in range(1, 11)]), False),
    ("window too short", capture_files(seconds=5), False),
    ("a silent loss", capture_files(rows=[{"seq": 1, "source_seq": 1, "source_lost": 0, "bytes": 60000, "generation": 1}, {"seq": 2, "source_seq": 5, "source_lost": 0, "bytes": 60000, "generation": 1}]), False),
    ("a flagged loss over the bound", capture_files(rows=[{"seq": 1, "source_seq": 1, "source_lost": 0, "bytes": 60000, "generation": 1}, {"seq": 2, "source_seq": 5, "source_lost": 3, "bytes": 60000, "generation": 1}]), False),
    ("the summary lies about the loss", capture_files(silent_loss=0, flagged_loss=7), False),
    ("a duplicate event", capture_files(rows=[{"seq": 1, "source_seq": 1, "source_lost": 0, "bytes": 60000, "generation": 1}] * 2), False),
    ("never resumed", capture_files(resumes=0), False),
    ("server lost events", capture_files(server_lost=4), False),
    ("two epochs", capture_files(epochs=["a", "b"]), False),
])
def test_capture_continuity(tmp_path, label, files, ok):
    assert (outcome(qc.capture_checks(run_dir(tmp_path, files), SPEC_U8, []))["capture_continuity"] == "OK") is ok, label


def test_a_missing_raw_file_is_unverified_never_ok(tmp_path):
    run = run_dir(tmp_path, {"unrelated.json": {}})
    for function, spec in ((qc.trigger_observations_checks, SPEC_T7), (qc.reenumeration_checks, {}), (qc.interruption_checks, SPEC_R5), (qc.expiry_checks, {"max_ttl_s": 60}),
                           (qc.identities_checks, {}), (qc.lan_observations_checks, {}), (qc.capture_checks, SPEC_U8)):
        assert {c.outcome for c in function(run, spec, [])} == {"MISSING"}, function.__name__


def test_unknown_scenario_under_v2_is_never_a_verified_pass_for_the_new_ones():
    for scenario in NEW:
        assert scenario not in V2["scenarios"]


# --- U8 : the archive must match its index ----------------------------------------------------------------------------
def _archive_verdict(tmp_path, mutate):
    files = capture_files()
    archive = files["_archive"]
    mutated = mutate(archive)
    run = run_dir(tmp_path, {k: v for k, v in files.items() if k != "_archive"})
    if mutated is not None:
        (tmp_path / "uart-archive.bin").write_bytes(mutated)
    return outcome(qc.capture_checks(run, SPEC_U8, []))["capture_continuity"]


@pytest.mark.parametrize("label,mutate,ok", [
    ("intact", lambda a: a, True),
    ("truncated", lambda a: a[:-1], False),
    ("extended", lambda a: a + b"x", False),
    ("one byte flipped", lambda a: a[:5] + bytes([a[5] ^ 1]) + a[6:], False),
    ("absent", lambda a: None, False),
    ("empty", lambda a: b"", False),
])
def test_u8_archive_matches_the_index(tmp_path, label, mutate, ok):
    assert (_archive_verdict(tmp_path, mutate) == "OK") is ok, label


def test_u8_archive_with_forged_summary_but_corrupt_chunk(tmp_path):
    """Rewriting the recorded size/SHA-256 to fit a corrupted archive is still caught by the per-chunk digests of the index."""
    files = capture_files()
    bad = files["_archive"][:5] + b"\xff" + files["_archive"][6:]
    files["capture-stats.json"].update(archive_bytes=len(bad), archive_sha256=hashlib.sha256(bad).hexdigest())
    files["_archive"] = bad
    assert outcome(qc.capture_checks(run_dir(tmp_path, files), SPEC_U8, []))["capture_continuity"] == "FAIL"
