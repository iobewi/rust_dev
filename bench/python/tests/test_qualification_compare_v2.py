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
import qualification_fixtures as qf                                     # noqa: E402
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


def edit_args(run, n, **changes):
    """Change the arguments the client recorded as SUBMITTED for operation n (a None value removes the key)."""
    def edit(rows):
        for r in rows:
            if r["seq"] == n:
                r["args"] = {**r["args"], **changes}
                r["args"] = {k: v for k, v in r["args"].items() if v is not None}
        return rows
    rewrite_jsonl(run / "board-0" / "commands.jsonl", edit)


def edit_images(run, n, edit):
    def go(rows):
        for r in rows:
            if r["seq"] == n:
                edit(r["args"]["images"])
        return rows
    rewrite_jsonl(run / "board-0" / "commands.jsonl", go)


def set_read(run, n, data):
    """Replace what read operation n returned, keeping blob and sidecar consistent (a plausible, self-consistent tamper)."""
    import hashlib
    for sidecar in (run / "board-0").glob("flash-*.json"):
        meta = json.loads(sidecar.read_text())
        if meta["operation"] == f"op{n}":
            sidecar.with_suffix(".bin").write_bytes(data)
            write_json(sidecar, {**meta, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            return
    raise AssertionError(f"no read for op{n}")


def drop_read(run, n):
    for sidecar in (run / "board-0").glob("flash-*.json"):
        if json.loads(sidecar.read_text())["operation"] == f"op{n}":
            sidecar.unlink()
            sidecar.with_suffix(".bin").unlink()


def edit_matches(run, **changes):
    rewrite_jsonl(run / "board-0" / "uart-matches.jsonl", lambda rows: [{**rows[0], **changes}])


def edit_cursor(run, **changes):
    rewrite_jsonl(run / "board-0" / "cursors.jsonl", lambda rows: [{**rows[0], **changes}])


def edit_console_event(run, **changes):
    rewrite_jsonl(run / "console-events.jsonl", lambda rows: [{**rows[0], **changes}])


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
    ("S3R-2", "hold_ms out of bounds", lambda run: edit_args(run, 0, hold_ms=5000), "hold_ms_bounded"),
    ("S3R-2", "two boot requests", lambda run: write_jsonl(run / "bootloader-requests.jsonl", [{"hold_ms": 500}, {"hold_ms": 500}]), "hold_ms_matches_request"),
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
    # --- raw-data correlation (PR #23 review): what was SUBMITTED, what was READ, which cursor, which operation ---
    ("S3R-1", "reset submitted with arguments", lambda run: edit_args(run, 0, hold_ms=5), "submitted_args_reset"),
    ("S3R-1", "commands.jsonl absent", lambda run: (run / "board-0" / "commands.jsonl").unlink(), "submitted_args_reset"),
    ("S3R-1", "no UART match record", lambda run: (run / "board-0" / "uart-matches.jsonl").unlink(), "uart_match_correlated"),
    ("S3R-1", "the matched pattern is another marker", lambda run: edit_matches(run, pattern_sha256="1" * 64), "uart_match_correlated"),
    ("S3R-1", "the recipe declared another marker", lambda run: rewrite_jsonl(run / "reset-requests.jsonl", lambda rows: [{**rows[0], "marker_sha256": "2" * 64}]), "uart_match_correlated"),
    ("S3R-1", "match recorded after another operation", lambda run: edit_matches(run, operation="op99"), "uart_match_correlated"),
    ("S3R-1", "matching event does not follow the cursor", lambda run: edit_matches(run, matched_seq=5), "uart_match_correlated"),
    ("S3R-1", "cursor taken after the submission", lambda run: edit_cursor(run, submitted=1), "uart_match_correlated"),
    ("S3R-1", "match of another generation", lambda run: edit_matches(run, generation="g0000009"), "uart_match_correlated"),
    ("S3R-1", "match of another epoch", lambda run: edit_matches(run, epoch="e0000009"), "uart_match_correlated"),
    ("S3R-1", "two match records", lambda run: rewrite_jsonl(run / "board-0" / "uart-matches.jsonl", lambda rows: rows + rows), "uart_match_correlated"),
    ("S3R-1", "operation not completed", lambda run: rewrite_jsonl(ops_path(run), lambda rows: [{**rows[0], "state": "indeterminate"}]), "uart_match_correlated"),
    ("S3R-2", "executed hold_ms is not an integer", lambda run: edit_args(run, 0, hold_ms="500"), "hold_ms_bounded"),
    ("S3R-2", "executed hold_ms differs from the request", lambda run: edit_args(run, 0, hold_ms=600), "hold_ms_matches_request"),
    ("S3R-2", "boot.download carries an extra argument", lambda run: edit_args(run, 0, return_console=True), "submitted_args_boot.download"),
    ("S3R-2", "hold_ms missing from the executed command", lambda run: edit_args(run, 0, hold_ms=None), "submitted_args_boot.download"),
    ("S3R-2", "marker matched is not the declared one", lambda run: edit_matches(run, pattern_sha256="3" * 64), "uart_match_correlated"),
    ("S3R-2", "marker too short to mean anything", lambda run: edit_matches(run, pattern_len=1), "uart_match_correlated"),
    ("S3R-3", "read did not request the console return", lambda run: edit_args(run, 0, return_console=False), "submitted_args_flash.read"),
    ("S3R-3", "console-events.jsonl absent", lambda run: (run / "console-events.jsonl").unlink(), "console_return_correlated"),
    ("S3R-3", "the event does not follow the cursor", lambda run: edit_console_event(run, seq=4), "console_return_correlated"),
    ("S3R-3", "cursor taken after the submission", lambda run: edit_cursor(run, submitted=1), "console_return_correlated"),
    ("S3R-3", "event of another operation", lambda run: edit_console_event(run, operation="op9"), "console_return_correlated"),
    ("S3R-3", "event is not a console opening", lambda run: edit_console_event(run, detail="port reopened later"), "console_return_correlated"),
    ("S3R-3", "two console openings", lambda run: rewrite_jsonl(run / "console-events.jsonl", lambda rows: rows + rows), "console_return_correlated"),
    ("R4", "commands.jsonl absent", lambda run: (run / "board-0" / "commands.jsonl").unlink(), "r4_images_match_manifest"),
    ("R4", "multi-image write carries one image only", lambda run: edit_images(run, 3, lambda imgs: imgs.pop()), "r4_images_match_manifest"),
    ("R4", "an image targets an unapproved address", lambda run: edit_images(run, 3, lambda imgs: imgs[1].update(address=0x300000)), "r4_images_match_manifest"),
    ("R4", "two images share one hash", lambda run: edit_images(run, 3, lambda imgs: imgs[1].update(sha256=imgs[0]["sha256"])), "r4_images_match_manifest"),
    ("R4", "an image equals the reference", lambda run: edit_images(run, 3, lambda imgs: imgs[0].update(sha256=qf.sha(qf.FF8K))), "r4_images_match_manifest"),
    ("R4", "the write asked for the console", lambda run: edit_args(run, 3, return_console=True), "r4_images_match_manifest"),
    ("R4", "a region read back differs from its image", lambda run: set_read(run, 4, b"\x33" * 8192), "r4_readbacks_equal_submitted_images"),
    ("R4", "no readback of a region after the write", lambda run: drop_read(run, 5), "r4_readbacks_equal_submitted_images"),
    ("R4", "restoration wrote another content", lambda run: edit_images(run, 7, lambda imgs: imgs[0].update(sha256="4" * 64)), "r4_restoration_matches_backups"),
    ("R4", "restoration of a region written twice", lambda run: edit_images(run, 9, lambda imgs: imgs[0].update(address=0x200000)), "r4_restoration_matches_backups"),
    ("R4", "comparing read differs from the backup", lambda run: set_read(run, 8, b"\x44" * 8192), "r4_restoration_matches_backups"),
    ("R4", "no comparing read after a restoration", lambda run: drop_read(run, 10), "r4_restoration_matches_backups"),
    ("R4", "a control window changed after the write", lambda run: set_read(run, 6, b"\x07" * 4096), "r4_control_windows_unchanged"),
    ("R4", "a control window changed after the restoration", lambda run: set_read(run, 11, b"\x07" * 4096), "r4_control_windows_unchanged"),
    ("R4", "a control window was erased before the first comparison", lambda run: set_read(run, 2, b"\xff" * 4096), "r4_control_windows_unchanged"),
    ("R4", "no post-write read of the window", lambda run: drop_read(run, 6), "r4_control_windows_unchanged"),
    ("R4", "no post-restore read of the window", lambda run: drop_read(run, 11), "r4_control_windows_unchanged"),
    ("R4", "window overlaps an approved region", lambda run: write_json(run / "r4-windows.json", [{"address": 0x200000, "size": 0x1000}]), "r4_control_windows_unchanged"),
    ("R4", "r4-windows.json absent", lambda run: (run / "r4-windows.json").unlink(), "r4_control_windows_unchanged"),
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
