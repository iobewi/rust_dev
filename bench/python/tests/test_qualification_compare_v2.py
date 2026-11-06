"""Criteria v2 (issue #2 phase 2: S3R-1, S3R-2, S3R-3, R4) in the evidence comparator. SIMULATED evidence only: no card, no server, no hardware.

Proves that the v2 criteria and the comparator reject incomplete or contradicted evidence for the new scenarios, that v1 is untouched and
that a scenario unknown to a criteria version can never be a verified PASS."""
import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qualification_compare as qc                                   # noqa: E402
from qualification_fixtures import make_run, write_json, write_jsonl   # noqa: E402

V1_PATH, V2_PATH = BENCH / "tools" / "qualification_criteria.json", BENCH / "tools" / "qualification_criteria_v2.json"
V1, V2 = json.loads(V1_PATH.read_text()), json.loads(V2_PATH.read_text())
V1_SHA = "2dfc47e20ff7e4e2fe7d5f62a0ae69387e66ae190e8a28ff4b1d68b80760760c"
NEW = ["S3R-1", "S3R-2", "S3R-3", "R4"]


def assess(path, criteria=V2):
    return qc.assess(qc.Run(path), criteria)


def failed(row, name):
    return [c for c in row["checks"] if c["name"] == name and c["outcome"] != "OK"]


def rewrite_jsonl(path, edit):
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    write_jsonl(path, edit(rows))


def rewrite_json(path, edit):
    doc = json.loads(path.read_text())
    edit(doc)
    write_json(path, doc)


def rewrite_report(run, edit):
    report = json.loads((run / "report.json").read_text())
    edit(report["tests"][0])
    write_json(run / "report.json", report)
    write_jsonl(run / "verdicts.jsonl", report["tests"])


# --- the versions ---------------------------------------------------------------------------------------------------
def test_v1_is_untouched_and_v2_extends_it_without_changing_any_v1_entry():
    assert qc.sha256_file(V1_PATH) == V1_SHA, "criteria v1 must never be edited"
    assert V2["version"] == 2
    assert {k: V2["scenarios"][k] for k in V1["scenarios"]} == V1["scenarios"]
    assert V2["requirements"][:len(V1["requirements"])] == V1["requirements"]
    assert V2["repeat_min"] == V1["repeat_min"] and V2["common"] == V1["common"]
    assert set(V2["scenarios"]) - set(V1["scenarios"]) == set(NEW)
    assert {s for r in V2["requirements"][len(V1["requirements"]):] for s in r["scenarios"]} == set(NEW)


def test_a_scenario_unknown_to_the_criteria_version_is_never_a_verified_pass(tmp_path):
    for scenario in NEW:
        row = assess(make_run(tmp_path, scenario, chip="esp32s3"), V1)
        assert row["recorded"] == "PASS" and row["effective"] == "INDETERMINATE" and row["verification"] == "UNVERIFIED", scenario
        assert failed(row, "scenario_criteria_known")


@pytest.mark.parametrize("scenario", NEW)
def test_valid_complete_evidence_is_a_verified_pass_under_v2(tmp_path, scenario):
    row = assess(make_run(tmp_path, scenario, chip="esp32s3"))
    assert (row["recorded"], row["effective"], row["verification"]) == ("PASS", "PASS", "VERIFIED"), row["reasons"]
    assert not row["reasons"]


@pytest.mark.parametrize("scenario", NEW)
@pytest.mark.parametrize("status", ["FAIL", "BLOCKED", "INDETERMINATE", "SKIP"])
def test_negative_and_inconclusive_verdicts_are_kept_as_recorded(tmp_path, scenario, status):
    row = assess(make_run(tmp_path, scenario, chip="esp32s3", status=status))
    assert row["recorded"] == row["effective"] == status


# --- mutations: every one must stop being a VERIFIED PASS ------------------------------------------------------------
def ops_path(run):
    return run / "board-0" / "operations.jsonl"


def add_op(command):
    def edit(rows):
        return rows + [{**rows[0], "id": f"extra-{command}", "command": command}]
    return edit


MUTATIONS = [
    # (scenario, label, mutate(run), failing check)
    ("S3R-1", "wrong chip in the manifest", lambda run: rewrite_json(run / "manifest.json", lambda d: d["boards"][0].update(chip="esp32c3")), "context_matches"),
    ("S3R-1", "wrong transport in the manifest", lambda run: rewrite_json(run / "manifest.json", lambda d: d["boards"][0].update(transports=["usb_serial_jtag"])), "context_matches"),
    ("S3R-1", "two resets", lambda run: rewrite_jsonl(ops_path(run), add_op("reset")), "exactly_1_reset"),
    ("S3R-1", "an interrupted window", lambda run: write_jsonl(run / "windows.jsonl", [{"window": 0, "event": "interrupted"}]), "no_continuity_break"),
    ("S3R-1", "a grant event", lambda run: rewrite_jsonl(run / "events.jsonl", lambda rows: rows + [{"seq": 9, "epoch": "e0000001", "at": 1.0, "kind": "grant", "seconds": 60}]), "no_grant_events"),
    ("S3R-1", "no ROM banner evidence", lambda run: rewrite_report(run, lambda t: t.update(evidence=[e for e in t["evidence"] if "ESP-ROM" not in e])), "rom_banner_same_window"),
    ("S3R-1", "banner of another chip", lambda run: rewrite_report(run, lambda t: t.update(evidence=[e.replace("esp32s3", "esp32c3") if "ESP-ROM" in e else e for e in t["evidence"]])), "rom_banner_same_window"),
    ("S3R-1", "a flash write sneaks in", lambda run: rewrite_jsonl(ops_path(run), add_op("flash.write")), "commands_allowed"),
    ("S3R-1", "transport not recorded", lambda run: (run / "transport.json").unlink(), "transport_declared_and_inferred"),
    ("S3R-2", "the grant was revoked (not a natural expiry)", lambda run: rewrite_jsonl(run / "events.jsonl", lambda rows: rows + [{"seq": 9, "epoch": "e0000001", "at": 2000.0, "kind": "grant", "seconds": 0}]), "natural_expiry"),
    ("S3R-2", "hold_ms out of bounds", lambda run: write_jsonl(run / "bootloader-requests.jsonl", [{"hold_ms": 5000}]), "hold_ms_bounded"),
    ("S3R-2", "two boot requests", lambda run: write_jsonl(run / "bootloader-requests.jsonl", [{"hold_ms": 500}, {"hold_ms": 500}]), "hold_ms_bounded"),
    ("S3R-2", "two boot.download operations", lambda run: rewrite_jsonl(ops_path(run), add_op("boot.download")), "exactly_1_boot.download"),
    ("S3R-2", "a reset instead", lambda run: rewrite_jsonl(ops_path(run), lambda rows: [{**rows[0], "command": "reset"}]), "commands_allowed"),
    ("S3R-2", "no download marker evidence", lambda run: rewrite_report(run, lambda t: t.update(evidence=[e for e in t["evidence"] if "download-mode marker" not in e])), "download_marker_same_window"),
    ("S3R-2", "no natural expiry evidence", lambda run: rewrite_report(run, lambda t: t.update(evidence=[e for e in t["evidence"] if "expired naturally" not in e])), "grant_expired_naturally"),
    ("S3R-2", "a revocation was requested", lambda run: write_jsonl(run / "operator-requests.jsonl", [{"request": "GRANT 60 s"}, {"request": "REVOKE it"}]), "operator_gestures_traced"),
    ("S3R-3", "a grant was involved", lambda run: rewrite_jsonl(run / "events.jsonl", lambda rows: rows + [{"seq": 9, "epoch": "e0000001", "at": 1.0, "kind": "grant", "seconds": 60}]), "no_grant_events"),
    ("S3R-3", "no console-return transport evidence", lambda run: rewrite_report(run, lambda t: t.update(evidence=[e for e in t["evidence"] if "console return shown" not in e])), "console_return_transport_event"),
    ("S3R-3", "two reads", lambda run: rewrite_jsonl(ops_path(run), add_op("flash.read")), "exactly_1_flash.read"),
    ("S3R-3", "server readback not released after verification", lambda run: rewrite_report(run, lambda t: t.update(evidence=[e for e in t["evidence"] if "deleted after" not in e])), "server_readback_released"),
    ("S3R-3", "a write sneaks in", lambda run: rewrite_jsonl(ops_path(run), add_op("flash.write")), "commands_allowed"),
    ("R4", "only one region approved", lambda run: rewrite_json(run / "manifest.json", lambda d: d.update(write_regions=d["write_regions"][:1])), "region_count"),
    ("R4", "a restoration write is missing", lambda run: rewrite_jsonl(ops_path(run), lambda rows: [r for r in rows if r["id"] != "op9"]), "flash_write_count"),
    ("R4", "a second multi-image write", lambda run: rewrite_jsonl(ops_path(run), add_op("flash.write")), "flash_write_count"),
    ("R4", "backups of a region differ", lambda run: rewrite_json(run / "board-0" / "ref-1-b.json", lambda d: d.update(sha256="0" * 64)), "backups_equal_per_region"),
    ("R4", "backup does not match the manifest region", lambda run: rewrite_json(run / "board-0" / "ref-0-a.json", lambda d: d.update(address=0x300000)), "backups_equal_per_region"),
    ("R4", "restoration not proven", lambda run: rewrite_report(run, lambda t: t.update(restoration="patterns may be present; restore required")), "restored_and_compared"),
    ("R4", "no post-write control window evidence", lambda run: rewrite_report(run, lambda t: t.update(evidence=[e for e in t["evidence"] if "after the write" not in e])), "windows_unchanged_after_write"),
    ("R4", "no post-restore control window evidence", lambda run: rewrite_report(run, lambda t: t.update(evidence=[e for e in t["evidence"] if "after the restoration" not in e])), "windows_unchanged_after_restore"),
    ("R4", "grant never revoked", lambda run: rewrite_jsonl(run / "events.jsonl", lambda rows: [r for r in rows if not (r.get("kind") == "grant" and r["seconds"] == 0)]), "grant_revoked_locally"),
    ("R4", "an interrupted window", lambda run: write_jsonl(run / "windows.jsonl", [{"window": 0, "event": "interrupted"}]), "no_continuity_break"),
    ("R4", "erase command sneaks in", lambda run: rewrite_jsonl(ops_path(run), add_op("flash.erase")), "commands_allowed"),
    ("R4", "server readback not released", lambda run: rewrite_report(run, lambda t: t.update(evidence=[e for e in t["evidence"] if "deleted after" not in e])), "server_readback_released"),
]


@pytest.mark.parametrize("scenario,label,mutate,check", MUTATIONS, ids=[f"{m[0]}:{m[1]}" for m in MUTATIONS])
def test_damaged_evidence_is_never_a_verified_pass(tmp_path, scenario, label, mutate, check):
    run = make_run(tmp_path, scenario, chip="esp32s3")
    assert assess(run)["verification"] == "VERIFIED"
    mutate(run)
    row = assess(run)
    assert row["recorded"] == "PASS" and row["effective"] == "INDETERMINATE", (label, row["reasons"])
    assert row["verification"] in {"CONTRADICTED", "UNVERIFIED"}
    assert failed(row, check), (label, [c for c in row["checks"] if c["outcome"] != "OK"])


def test_s3r_scenarios_needing_no_operator_gesture_do_not_require_a_requests_file(tmp_path):
    for scenario in ("S3R-1", "S3R-3"):
        run = make_run(tmp_path, scenario, chip="esp32s3")
        assert not (run / "operator-requests.jsonl").exists()
        assert assess(run)["verification"] == "VERIFIED"


# --- campaign level --------------------------------------------------------------------------------------------------
def test_v2_matrix_lists_the_new_requirements_and_one_pass_is_functional_not_repeatable(tmp_path):
    root = tmp_path / "evidence"
    for scenario in NEW:
        make_run(root, scenario, chip="esp32s3")
    runs = qc.analyse([("c", "candidate", root)], V2)
    matrix = {r["id"]: r for r in qc.matrix(runs, V2)}
    for req in ("REQ-08", "REQ-09", "REQ-10", "REQ-11"):
        ctx = matrix[req]["contexts"]["esp32s3/external_uart"]
        assert ctx["functional"] == "DEMONSTRATED" and ctx["repeatable"] == "NOT_DEMONSTRATED", req


def test_the_cli_accepts_v2_and_pins_it(tmp_path):
    root = tmp_path / "evidence"
    for scenario in NEW:
        make_run(root, scenario, chip="esp32s3")
    out = tmp_path / "out"
    argv = ["--criteria", str(V2_PATH), "--criteria-sha256", qc.sha256_file(V2_PATH), "--root", f"e:candidate={root}", "--output", str(out), "--strict"]
    assert qc.main(argv) == 0
    report = json.loads((out / "comparison.json").read_text())
    assert report["criteria_sha256"] == qc.sha256_file(V2_PATH)
    assert {r["scenario"]: r["effective"] for r in report["runs"]} == {s: "PASS" for s in NEW}
    with pytest.raises(SystemExit, match="criteria changed"):
        qc.main(["--criteria", str(V2_PATH), "--criteria-sha256", V1_SHA, "--root", f"e:candidate={root}", "--output", str(tmp_path / "out2")])
