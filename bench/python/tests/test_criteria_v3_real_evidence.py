"""Criteria v3 read the evidence the REAL recipes write against the RAM simulator: every scenario-specific check must be satisfied by what the
client and the recipes actually record (a SIMULATED run is never a qualification; the provenance of these runs is dirty on purpose)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_final_recipes as final                                         # noqa: E402
from test_final_recipes import (CAPTURE, FAULT, lan_target, prepared, run_recipe, sim, sim_two)   # noqa: E402,F401  (fixtures)

BENCH = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCH / "tools"))
import qualification_compare as qc                                         # noqa: E402

V3 = json.loads((BENCH / "tools" / "qualification_criteria_v3.json").read_text())
ENVIRONMENT_CHECKS = {"provenance", "versions_recorded", "hardware_grade"}      # a dirty simulated tree / no hardware stamps: expected to differ


def judge(tmp_path, scenario):
    dirs = sorted((tmp_path / "evidence").glob(f"{scenario}-*"))
    assert len(dirs) == 1, dirs
    row = qc.assess(qc.Run(dirs[0]), V3)
    bad = [(c["name"], c["outcome"], c["detail"]) for c in row["checks"] if c["outcome"] != "OK" and c["severity"] == "required" and c["name"] not in ENVIRONMENT_CHECKS]
    assert not bad, bad
    return row


def test_nusb_scenarios(sim, tmp_path, monkeypatch):
    url, _ = prepared(sim, monkeypatch, {"SIM_NATIVE": "1"})
    for selector, scenario in (("test_nusb0_identity_and_refusals", "NUSB-0"), ("test_nusb1_reads_and_table", "NUSB-1")):
        sub = tmp_path / scenario
        proc, reports = run_recipe(sub, url, f"test_native_usb.py::{selector}")
        assert reports[scenario]["status"] == "PASS", proc.stdout[-800:]
        judge(sub, scenario)


@pytest.mark.parametrize("reenumerate", [False, True])
def test_nusb2_both_outcomes(sim, tmp_path, monkeypatch, reenumerate):
    url, _ = prepared(sim, monkeypatch, {"SIM_NATIVE": "1", **({"SIM_REENUMERATE": "1"} if reenumerate else {})})
    proc, reports = run_recipe(tmp_path, url, "test_native_usb.py::test_nusb2_reset_and_reenumeration", "--qual-allow-reset")
    assert reports["NUSB-2"]["status"] == "PASS", proc.stdout[-800:]
    judge(tmp_path, "NUSB-2")


def test_trigger_scenario(sim, tmp_path):
    url, _ = sim([[6, 600], [50, 0]], chip="esp32s3")
    proc, reports = run_recipe(tmp_path, url, "test_triggers.py", transport="external_uart", accept_unknown_transport=True)
    assert reports["T7"]["status"] == "PASS", proc.stdout[-800:]
    judge(tmp_path, "T7")


def test_interruption_scenario(sim, tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_DROP_DURING_READ", "1")
    url, _ = sim([[8, "recover"]], chip="esp32s3")
    proc, reports = run_recipe(tmp_path, url, "test_interruption.py::test_r5_reload", transport="external_uart", accept_unknown_transport=True, faults=FAULT)
    assert reports["R5-RELOAD"]["status"] == "PASS", proc.stdout[-800:]
    judge(tmp_path, "R5-RELOAD")


def test_reservation_expiry_scenario(sim, tmp_path):
    url, _ = sim([], chip="esp32s3")
    proc, reports = run_recipe(tmp_path, url, "test_reservation_expiry.py", transport="external_uart", accept_unknown_transport=True)
    assert reports["G9"]["status"] == "PASS", proc.stdout[-800:]
    judge(tmp_path, "G9")


def test_two_boards_scenario(sim_two, tmp_path):
    url, _ = sim_two()
    proc, reports = run_recipe(tmp_path, url, "test_two_boards.py", transport="external_uart", accept_unknown_transport=True, second_board={"board_id": "board2"})
    assert reports["M2"]["status"] == "PASS", proc.stdout[-800:]
    judge(tmp_path, "M2")


def test_lan_http_scenario(sim, tmp_path, monkeypatch, lan_target):
    monkeypatch.setenv("SIM_HTTP_ORIGINS", lan_target)
    url, _ = sim([[6, 600], [40, 0]], chip="esp32s3")
    proc, reports = run_recipe(tmp_path, url, "test_lan_http.py", transport="external_uart", accept_unknown_transport=True, lan_http={"origin": lan_target})
    assert reports["H10"]["status"] == "PASS", proc.stdout[-800:]
    judge(tmp_path, "H10")


def test_capture_scenario_meets_the_pinned_thresholds(sim, tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_UART_RATE", "100:1024")
    url, _ = sim([], chip="esp32s3")
    window = {"duration_s": 31, "min_bytes": 100000, "resume_every_s": 8, "max_flagged_loss": 0}
    proc, reports = run_recipe(tmp_path, url, "test_uart_capture.py", transport="external_uart", accept_unknown_transport=True, capture=window)
    assert reports["U8"]["status"] == "PASS", proc.stdout[-800:]
    judge(tmp_path, "U8")
