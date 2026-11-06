"""Tests of the evidence comparator with SIMULATED evidence only. No card, no server, no hardware."""
import json
import shutil
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qualification_compare as qc                                   # noqa: E402
from qualification_fixtures import (CONTEXTS, FF8K, make_run, refusal, sha, tree_digest, write_json,   # noqa: E402
                                    write_jsonl)

CRITERIA_PATH = BENCH / "tools" / "qualification_criteria.json"
CRITERIA = json.loads(CRITERIA_PATH.read_text())
S3_SCENARIOS = ["Q1", "Q2a", "Q2b", "Q2c", "Q2d", "Q3"]
C3_SCENARIOS = ["C3R-0", "C3R-1", "C3R-2", "C3R-3", "Q1", "Q2a", "Q2b", "Q2c", "Q2d", "Q3"]


def assess(path):
    return qc.assess(qc.Run(path), CRITERIA)


def failing(row, name):
    return [c for c in row["checks"] if c["name"] == name and c["outcome"] != "OK"]


# --- valid and complete evidence ----------------------------------------------------------------------------------
@pytest.mark.parametrize("chip,scenario", [("esp32s3", s) for s in S3_SCENARIOS] + [("esp32c3", s) for s in C3_SCENARIOS])
def test_valid_complete_evidence_is_verified_pass(tmp_path, chip, scenario):
    row = assess(make_run(tmp_path, scenario, chip=chip))
    assert (row["recorded"], row["effective"], row["verification"]) == ("PASS", "PASS", "VERIFIED"), row["reasons"]
    assert row["scenario"] == scenario and not row["reasons"]


# --- missing evidence never produces a PASS -------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["after.json", "provenance.json", "events.jsonl", "requests.jsonl", "manifest.json", "recipe.py", "cleanup.json"])
def test_a_missing_required_file_turns_a_recorded_pass_into_indeterminate(tmp_path, name):
    run = make_run(tmp_path, "Q2a")
    (run / name).unlink()
    row = assess(run)
    assert row["recorded"] == "PASS" and row["effective"] == "INDETERMINATE" and row["verification"] == "UNVERIFIED", row["reasons"]
    assert row["reasons"]


@pytest.mark.parametrize("scenario,name", [("Q3", "windows.jsonl"), ("Q3", "abandoned-handles.jsonl"), ("C3R-0", "transport.json"),
                                           ("C3R-2", "table-probes.json"), ("C3R-3", "reset-requests.jsonl"), ("C3R-0", "identity.json")])
def test_a_missing_scenario_file_is_not_a_pass(tmp_path, scenario, name):
    run = make_run(tmp_path, scenario, chip="esp32c3")
    (run / name).unlink()
    assert assess(run)["effective"] == "INDETERMINATE"


def test_missing_evidence_lines_or_unreadable_json_are_not_a_pass(tmp_path):
    run = make_run(tmp_path, "Q1")
    test = json.loads((run / "report.json").read_text())
    test["tests"][0]["evidence"] = []
    write_json(run / "report.json", test)
    assert assess(run)["effective"] == "INDETERMINATE"
    run2 = make_run(tmp_path, "Q2b", ts="20261106T100001Z")
    (run2 / "events.jsonl").write_text("{ not json")
    assert assess(run2)["effective"] == "INDETERMINATE"


# --- hashes ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("target,check", [("recipe.py", "recipe_hash"), ("support/session.py", "support_hashes"), ("client.py", "client_source_hashes"),
                                          ("qualification.py", "qualification_module_hash"), ("board-0/ref-a.bin", "flash_sidecars"),
                                          ("board-0/flash-001-00000000.bin", "flash_sidecars")])
def test_a_wrong_sha256_contradicts_the_recorded_pass(tmp_path, target, check):
    run = make_run(tmp_path, "Q1")
    path = run / target
    path.write_bytes(path.read_bytes() + b"tampered")
    row = assess(run)
    assert row["verification"] == "CONTRADICTED" and row["effective"] == "INDETERMINATE", row["reasons"]
    assert failing(row, check)


def test_a_modified_manifest_contradicts_its_recorded_digest(tmp_path):
    run = make_run(tmp_path, "Q1")
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["write_regions"][0]["size"] = 4096
    write_json(run / "manifest.json", manifest)
    assert failing(assess(run), "manifest_hash")


def test_a_backup_whose_sidecar_digest_is_wrong_is_detected(tmp_path):
    run = make_run(tmp_path, "Q1")
    meta = json.loads((run / "board-0/ref-b.json").read_text())
    meta["sha256"] = "0" * 64
    write_json(run / "board-0/ref-b.json", meta)
    row = assess(run)
    assert failing(row, "flash_sidecars") and failing(row, "backups_equal")


# --- inconsistent metadata ------------------------------------------------------------------------------------------
def test_provenance_must_be_clean_complete_and_hardware_grade(tmp_path):
    assert failing(assess(make_run(tmp_path, "Q2a", clean=False)), "provenance")
    run = make_run(tmp_path, "Q2a", ts="20261106T100001Z")
    provenance = json.loads((run / "provenance.json").read_text())
    provenance["dirty_allowed_by_option"] = True
    write_json(run / "provenance.json", provenance)
    assert failing(assess(run), "provenance")
    run = make_run(tmp_path, "Q2a", ts="20261106T100002Z")
    provenance = json.loads((run / "provenance.json").read_text())
    provenance["git_sha"] = "not-a-sha"
    write_json(run / "provenance.json", provenance)
    assert failing(assess(run), "provenance")


def test_missing_versions_are_unverifiable(tmp_path):
    run = make_run(tmp_path, "Q2a")
    before = json.loads((run / "before.json").read_text())
    del before["server_observed"]["server_sha256"]
    write_json(run / "before.json", before)
    row = assess(run)
    assert failing(row, "versions_recorded") and row["effective"] == "INDETERMINATE"


@pytest.mark.parametrize("mutate,check", [
    (lambda r: r.update(incidents=["[bench] something"]), "status_consistency"),
])
def test_a_pass_with_incidents_or_cleanup_errors_is_contradictory(tmp_path, mutate, check):
    run = make_run(tmp_path, "Q2a")
    report = json.loads((run / "report.json").read_text())
    mutate(report["tests"][0])
    write_json(run / "report.json", report)
    assert failing(assess(run), check)
    run2 = make_run(tmp_path, "Q2a", ts="20261106T100001Z")
    write_json(run2 / "cleanup.json", {"errors": ["BenchError"], "submissions": {"primary": 3}})
    assert failing(assess(run2), "status_consistency")


def test_verdicts_jsonl_must_agree_with_the_report(tmp_path):
    run = make_run(tmp_path, "Q2a")
    rows = [json.loads(l) for l in (run / "verdicts.jsonl").read_text().splitlines()]
    rows[-1]["status"] = "FAIL"
    write_jsonl(run / "verdicts.jsonl", rows)
    assert failing(assess(run), "status_consistency")


@pytest.mark.parametrize("field,value", [("reservation", {"owner": "claude"}), ("privileged_seconds", 120), ("quarantined", True), ("connected", False)])
def test_a_pass_must_end_on_an_idle_card(tmp_path, field, value):
    run = make_run(tmp_path, "Q2a")
    after = json.loads((run / "after.json").read_text())
    after["device"][field] = value
    write_json(run / "after.json", after)
    assert failing(assess(run), "final_state") and assess(run)["effective"] == "INDETERMINATE"


# --- epoch and USB generation changes -----------------------------------------------------------------------------
def test_a_generation_or_epoch_change_contradicts_a_pass_that_promised_continuity(tmp_path):
    for field, key in (("generation", "device"), ("epoch", None)):
        run = make_run(tmp_path, "C3R-3", chip="esp32c3", ts=f"2026110610000{field[0]}Z")
        after = json.loads((run / "after.json").read_text())
        if key:
            after["device"]["generation"] = "g-reenumerated"
        else:
            after["epoch"] = "e-other"
        write_json(run / "after.json", after)
        row = assess(run)
        assert failing(row, "continuity") and row["effective"] == "INDETERMINATE", field


def test_q3_must_change_both_the_epoch_and_the_generation(tmp_path):
    run = make_run(tmp_path, "Q3")
    after = json.loads((run / "after.json").read_text())
    after["epoch"] = json.loads((run / "before.json").read_text())["epoch"]
    write_json(run / "after.json", after)
    assert failing(assess(run), "continuity")


def test_c3_reset_with_a_usb_reenumeration_recorded_indeterminate_is_preserved_not_upgraded(tmp_path):
    run = make_run(tmp_path, "C3R-3", chip="esp32c3", status="INDETERMINATE")
    write_jsonl(run / "windows.jsonl", [{"event": "interrupted"}, {"event": "opened"}])
    after = json.loads((run / "after.json").read_text())
    after["device"]["generation"] = "g-reenumerated"
    write_json(run / "after.json", after)
    row = assess(run)
    assert (row["recorded"], row["effective"]) == ("INDETERMINATE", "INDETERMINATE")


# --- contradictory results ------------------------------------------------------------------------------------------
def test_q2a_recorded_pass_with_a_revocation_is_contradicted(tmp_path):
    run = make_run(tmp_path, "Q2a")
    write_jsonl(run / "events.jsonl", [{"window": 0}, {"kind": "grant", "at": 1010.0, "seconds": 60}, {"kind": "grant", "at": 1037.0, "seconds": 0}])
    row = assess(run)
    assert failing(row, "natural_expiry") and row["effective"] == "INDETERMINATE"


def test_q2b_without_a_genuine_revocation_is_not_a_pass(tmp_path):
    run = make_run(tmp_path, "Q2b")
    write_jsonl(run / "events.jsonl", [{"window": 0}, {"kind": "grant", "at": 1010.0, "seconds": 300}, {"kind": "grant", "at": 1305.0, "seconds": 0}])
    assert failing(assess(run), "local_revocation")


@pytest.mark.parametrize("scenario,code,client", [("Q2c", "grant_required", "primary"), ("Q2d", "trigger_not_owned", "primary"), ("Q2c", "trigger_not_owned", "secondary-1")])
def test_q2_ownership_checks_need_the_right_refusal_from_the_right_client(tmp_path, scenario, code, client):
    run = make_run(tmp_path, scenario)
    write_jsonl(run / "requests.jsonl", [{"at": 1000.0, "client": "primary", "method": "GET", "path": "/health", "ok": True}, refusal(1020.0, client, code)])
    row = assess(run)
    assert row["effective"] == "INDETERMINATE" and row["verification"] == "CONTRADICTED", row["reasons"]


def test_an_ownership_refusal_outside_a_valid_grant_could_be_a_grant_refusal(tmp_path):
    run = make_run(tmp_path, "Q2c")
    write_jsonl(run / "events.jsonl", [{"window": 0}, {"kind": "grant", "at": 1010.0, "seconds": 300}, {"kind": "grant", "at": 1015.0, "seconds": 0}])
    assert failing(assess(run), "refused_with_valid_grant")                    # revoked before the refusal at t=1020
    run2 = make_run(tmp_path, "Q2c", ts="20261106T100001Z")
    write_jsonl(run2 / "events.jsonl", [{"window": 0}, {"kind": "grant", "at": 1010.0, "seconds": 5}])
    assert failing(assess(run2), "refused_with_valid_grant")                   # expired (5 s) before the refusal


def test_q2d_needs_two_distinct_clients(tmp_path):
    run = make_run(tmp_path, "Q2d")
    write_json(run / "cleanup.json", {"errors": [], "submissions": {"primary": 2}})
    assert failing(assess(run), "distinct_clients")


def test_q1_with_different_backups_or_a_foreign_region_is_contradicted(tmp_path):
    run = make_run(tmp_path, "Q1")
    other = b"\x00" * 8192
    (run / "board-0/ref-b.bin").write_bytes(other)
    write_json(run / "board-0/ref-b.json", {"address": 0x400000, "size": 8192, "sha256": sha(other), "complete": True})
    assert failing(assess(run), "backups_equal")
    run2 = make_run(tmp_path, "Q1", ts="20261106T100001Z")
    for name in ("ref-a", "ref-b"):
        write_json(run2 / f"board-0/{name}.json", {"address": 0x500000, "size": 8192, "sha256": sha(FF8K), "complete": True})
    assert failing(assess(run2), "region_matches_manifest")


def test_a_command_outside_the_scenario_contract_is_contradicted(tmp_path):
    run = make_run(tmp_path, "C3R-1", chip="esp32c3")
    with (run / "board-0/operations.jsonl").open("a") as stream:
        stream.write(json.dumps({"id": "opx", "command": "flash.write", "state": "completed", "settled": True}) + "\n")
    row = assess(run)
    assert failing(row, "commands_allowed") and row["effective"] == "INDETERMINATE"


def test_c3_reset_pass_must_have_no_interrupted_window(tmp_path):
    run = make_run(tmp_path, "C3R-3", chip="esp32c3")
    write_jsonl(run / "windows.jsonl", [{"event": "interrupted"}])
    assert failing(assess(run), "no_continuity_break")


def test_a_pass_whose_recipe_predates_artifact_release_gets_an_advisory_note_only(tmp_path):
    run = make_run(tmp_path, "C3R-1", chip="esp32c3")
    report = json.loads((run / "report.json").read_text())
    report["tests"][0]["evidence"] = [e for e in report["tests"][0]["evidence"] if "server readback" not in e]
    write_json(run / "report.json", report)
    write_jsonl(run / "verdicts.jsonl", report["tests"])
    row = assess(run)
    assert row["effective"] == "PASS" and any(c["name"] == "server_readbacks_released" and c["outcome"] == "FAIL" and c["severity"] == "advisory" for c in row["checks"])


# --- operator gestures are traced -----------------------------------------------------------------------------------
@pytest.mark.parametrize("scenario", ["Q1", "Q2a", "Q2b", "Q2c", "Q2d", "Q3"])
def test_a_scenario_that_needs_gestures_must_have_them_recorded(tmp_path, scenario):
    run = make_run(tmp_path, scenario)
    (run / "operator-requests.jsonl").unlink()
    row = assess(run)
    assert failing(row, "operator_gestures_traced") and row["effective"] == "INDETERMINATE"


def test_gesture_requests_must_match_the_grants_and_revocations_actually_observed(tmp_path):
    run = make_run(tmp_path, "Q2b")
    write_jsonl(run / "operator-requests.jsonl", [{"request": "GRANT 300 s locally; then wait for the revoke request"}])   # no REVOKE request, but a zero event
    assert failing(assess(run), "operator_gestures_traced")
    run2 = make_run(tmp_path, "Q2a", ts="20261106100001Z")
    write_jsonl(run2 / "events.jsonl", [{"window": 0}, {"kind": "grant", "at": 1010.0, "seconds": 60}, {"kind": "grant", "at": 1020.0, "seconds": 60}])
    assert failing(assess(run2), "operator_gestures_traced")                                            # two grants for one request


# --- history: negative and inconclusive results are kept as recorded --------------------------------------------------
@pytest.mark.parametrize("status", ["FAIL", "BLOCKED", "INDETERMINATE", "SKIP"])
def test_historical_non_pass_results_are_never_reinterpreted(tmp_path, status):
    run = make_run(tmp_path, "Q2a", status=status)
    row = assess(run)
    assert row["recorded"] == status and row["effective"] == status


def test_a_negative_with_missing_files_stays_negative_and_is_flagged_not_hidden(tmp_path):
    run = make_run(tmp_path, "Q3", status="INDETERMINATE")
    (run / "after.json").unlink()                                              # the server died: no final snapshot
    row = assess(run)
    assert (row["recorded"], row["effective"], row["verification"]) == ("INDETERMINATE", "INDETERMINATE", "UNVERIFIED")
    assert row["reasons"]


def test_the_matrix_lists_historical_negatives_without_counting_them_as_pass(tmp_path):
    hist, new = tmp_path / "hist", tmp_path / "new"
    make_run(hist, "Q2a", status="FAIL", ts="20261106T090000Z")
    make_run(hist, "Q2a", status="INDETERMINATE", ts="20261106T090100Z")
    make_run(new, "Q2a", ts="20261106T100000Z")
    runs = qc.analyse([("hist", "historic", hist), ("new", "candidate", new)], CRITERIA)
    row = next(r for r in qc.matrix(runs, CRITERIA) if r["id"] == "REQ-02")
    counts = row["contexts"]["esp32s3/external_uart"]["scenarios"]["Q2a"]["effective"]
    assert counts["FAIL"] == 1 and counts["INDETERMINATE"] == 1 and counts["PASS"] == 1


# --- repeatability, inter-card ----------------------------------------------------------------------------------------
def candidate_runs(tmp_path, count, scenario="Q2a", chip="esp32s3", label="c"):
    root = tmp_path / label
    for i in range(count):
        make_run(root, scenario, chip=chip, ts=f"2026110611000{i}Z")
    return root


def req_for(runs, requirement):
    return next(r for r in qc.matrix(runs, CRITERIA) if r["id"] == requirement)


def test_three_identical_verified_candidate_passes_demonstrate_repeatability(tmp_path):
    runs = qc.analyse([("c", "candidate", candidate_runs(tmp_path, 3))], CRITERIA)
    ctx = req_for(runs, "REQ-02")["contexts"]["esp32s3/external_uart"]
    assert ctx["functional"] == "DEMONSTRATED" and ctx["repeatable"] == "DEMONSTRATED"


def test_two_passes_are_not_enough_and_historic_passes_do_not_count(tmp_path):
    runs = qc.analyse([("c", "candidate", candidate_runs(tmp_path, 2))], CRITERIA)
    assert req_for(runs, "REQ-02")["contexts"]["esp32s3/external_uart"]["repeatable"] == "NOT_DEMONSTRATED"
    runs = qc.analyse([("h", "historic", candidate_runs(tmp_path, 3, label="h"))], CRITERIA)
    ctx = req_for(runs, "REQ-02")["contexts"]["esp32s3/external_uart"]
    assert ctx["functional"] == "DEMONSTRATED" and ctx["repeatable"] == "NOT_DEMONSTRATED"


def test_a_behavioural_drift_between_passes_breaks_repeatability_and_is_reported(tmp_path):
    root = candidate_runs(tmp_path, 3)
    drifting = sorted(root.glob("Q2a-*"))[2]
    with (drifting / "board-0/operations.jsonl").open("a") as stream:
        stream.write(json.dumps({"id": "extra", "command": "mark", "state": "completed", "settled": True, "unsettled": False}) + "\n")
    runs = qc.analyse([("c", "candidate", root)], CRITERIA)
    assert req_for(runs, "REQ-02")["contexts"]["esp32s3/external_uart"]["repeatable"] == "NOT_DEMONSTRATED"
    group = qc.repeatability(runs)[0]
    assert group["stable"] is False and any("behaviour.commands" in d for d in group["drift"])


def test_a_changed_recipe_or_server_between_passes_is_environment_drift(tmp_path):
    root = tmp_path / "c"
    make_run(root, "Q2a", ts="20261106110000Z")
    make_run(root, "Q2a", ts="20261106110001Z", recipe_text="# another recipe\n")
    provenance = json.loads((root / "Q2a-20261106110001Z/provenance.json").read_text())
    provenance["git_sha"] = "1" * 40
    write_json(root / "Q2a-20261106110001Z/provenance.json", provenance)
    runs = qc.analyse([("c", "candidate", root)], CRITERIA)
    assert any("environment.recipe_git_sha" in d for g in qc.repeatability(runs) for d in g["drift"])


def test_inter_card_comparison_ignores_card_specific_values_and_flags_behaviour(tmp_path):
    root = tmp_path / "both"
    make_run(root, "Q1", chip="esp32s3", ts="20261106110000Z")
    make_run(root, "Q1", chip="esp32c3", ts="20261106110001Z")
    runs = qc.analyse([("both", "candidate", root)], CRITERIA)
    assert qc.inter_card(runs)[0]["behaviour_differences"] == []
    ops = root / "Q1-20261106110001Z/board-0/operations.jsonl"
    lines = ops.read_text().splitlines()
    ops.write_text("\n".join(reversed(lines)) + "\n")                     # same allowed commands, different order = behaviour drift
    runs = qc.analyse([("both", "candidate", root)], CRITERIA)
    assert all(r["effective"] == "PASS" for r in runs)
    assert any("commands" in d for d in qc.inter_card(runs)[0]["behaviour_differences"])


# --- the tool itself --------------------------------------------------------------------------------------------------
def run_cli(tmp_path, roots, *extra):
    args = ["--criteria", str(CRITERIA_PATH), "--output", str(tmp_path / "out")]
    for root in roots:
        args += ["--root", root]
    return qc.main(args + list(extra))


def test_the_tool_never_modifies_the_evidence_it_reads(tmp_path):
    root = tmp_path / "evidence"
    for s in S3_SCENARIOS:
        make_run(root, s)
    before = tree_digest(root)
    assert run_cli(tmp_path, [f"e:candidate={root}"]) == 0
    assert tree_digest(root) == before
    assert (tmp_path / "out/comparison.json").is_file() and (tmp_path / "out/comparison.md").is_file()


def test_the_output_directory_cannot_be_inside_an_evidence_root(tmp_path):
    root = tmp_path / "evidence"
    make_run(root, "Q2a")
    with pytest.raises(SystemExit, match="must not be inside"):
        qc.main(["--criteria", str(CRITERIA_PATH), "--output", str(root / "report"), "--root", f"e:candidate={root}"])


def test_criteria_can_be_pinned_and_a_change_is_refused(tmp_path):
    root = tmp_path / "evidence"
    make_run(root, "Q2a")
    assert run_cli(tmp_path, [f"e:candidate={root}"], "--criteria-sha256", qc.sha256_file(CRITERIA_PATH)) == 0
    with pytest.raises(SystemExit, match="criteria changed"):
        run_cli(tmp_path, [f"e:candidate={root}"], "--criteria-sha256", "0" * 64)


def test_strict_mode_fails_when_a_recorded_pass_cannot_be_verified(tmp_path, capsys):
    root = tmp_path / "evidence"
    run = make_run(root, "Q2a")
    assert run_cli(tmp_path, [f"e:candidate={root}"], "--strict") == 0
    (run / "provenance.json").unlink()
    assert run_cli(tmp_path, [f"e:candidate={root}"], "--strict") == 1
    assert "could not be verified" in capsys.readouterr().err
    assert json.loads((tmp_path / "out/comparison.json").read_text())["runs"][0]["effective"] == "INDETERMINATE"


def test_report_marks_simulated_evidence_and_lists_what_is_not_covered(tmp_path):
    root = tmp_path / "evidence"
    for s in S3_SCENARIOS:
        make_run(root, s)
    run_cli(tmp_path, [f"e:candidate={root}"], "--simulated")
    text = (tmp_path / "out/comparison.md").read_text()
    assert "SIMULATED EVIDENCE" in text and "fault_injection" in text and "endurance" in text and "certification" in text
    data = json.loads((tmp_path / "out/comparison.json").read_text())
    assert data["simulated"] is True and data["criteria_sha256"] == qc.sha256_file(CRITERIA_PATH)


def test_every_scenario_in_the_requirement_catalogue_has_criteria():
    scenarios = {s for r in CRITERIA["requirements"] for s in r["scenarios"]}
    assert scenarios <= set(CRITERIA["scenarios"]) and {n["level"] for n in CRITERIA["not_covered"]} == {"fault_injection", "endurance", "consumer_firmware", "certification"}
